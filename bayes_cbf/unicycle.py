import random
import math
import logging
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
from functools import partial
from collections import namedtuple

from scipy.special import erfinv
import torch
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch

from bayes_cbf.misc import (t_hstack, store_args, DynamicsModel,
                            ZeroDynamicsModel, epsilon, to_numpy,
                            get_affine_terms, get_quadratic_terms, t_jac,
                            variable_required_grad,
                            create_summary_writer, plot_to_image)
from bayes_cbf.gp_algebra import (DeterministicGP,)
from bayes_cbf.cbc1 import RelDeg1Safety
from bayes_cbf.car.vis import CarWithObstacles
from bayes_cbf.sampling import sample_generator_trajectory, Visualizer
from bayes_cbf.plotting import (plot_learned_2D_func, plot_results,
                                draw_ellipse, var_to_scale_theta)
from bayes_cbf.control_affine_model import ControlAffineRegressor
from bayes_cbf.controllers import (ControlCBFLearned, NamedAffineFunc,
                                   TensorboardPlotter, LQRController,
                                   GreedyController, ILQRController,
                                   ZeroController, Controller)
from bayes_cbf.ilqr import ILQR
from bayes_cbf.planner import PiecewiseLinearPlanner, Planner


class UnicycleDynamicsModel(DynamicsModel):
    """
    Ẋ     =     f(X)     +   g(X)     u

    [ ẋ  ] = [ 0 ] + [ cos(θ), 0 ] [ v ]
    [ ẏ  ]   [ 0 ]   [ sin(θ), 0 ] [ ω ]
    [ θ̇  ]   [ 0 ]   [ 0,      1 ]
    """
    def __init__(self):
        super().__init__()
        self.m = 2 # [v, ω]
        self.n = 3 # [x, y, θ]

    @property
    def ctrl_size(self):
        return self.m

    @property
    def state_size(self):
        return self.n

    def f_func(self, X_in):
        """
                [ 0   ]
         f(x) = [ 0   ]
                [ 0   ]
        """
        return X_in.new_zeros(X_in.shape) * X_in

    def g_func(self, X_in):
        """
                [ cos(θ), 0 ]
         g(x) = [ sin(θ), 0 ]
                [ 0,      1 ]
        """
        X = X_in.unsqueeze(0) if X_in.dim() <= 1 else X_in
        gX = torch.zeros((*X.shape, self.m))
        θ = X[..., 2:3]
        θ = θ.unsqueeze(-1)
        zero = θ.new_zeros((*X.shape[:-1], 1, 1))
        ones = θ.new_ones((*X.shape[:-1], 1, 1))
        gX = torch.cat([torch.cat([θ.cos(), zero], dim=-1),
                        torch.cat([θ.sin(), zero], dim=-1),
                        torch.cat([zero, ones], dim=-1)],
                       dim=-2)
        return gX.squeeze(0) if X_in.dim() <= 1 else gX

    def normalize_state(self, X_in):
        X_in[..., 2] = normalize_radians(X_in[..., 2])
        return X_in


Obstacle = namedtuple('Obstacle', 'center radius'.split())


class ObstacleCBF(RelDeg1Safety, NamedAffineFunc):
    """
    ∇h(x)ᵀf(x) + ∇h(x)ᵀg(x)u + γ_c h(x) > 0
    """
    @partial(store_args, skip=["model", "max_unsafe_prob"])
    def __init__(self, model, obstacle : Obstacle, γ_c=40, name="obstacle_cbf",
                 dtype=torch.get_default_dtype(),
                 max_unsafe_prob=0.01):
        self._model = model
        self._max_unsafe_prob = max_unsafe_prob
        self.center = torch.tensor(obstacle.center, dtype=dtype)
        self.radius = torch.tensor(obstacle.radius, dtype=dtype)

    @property
    def model(self):
        return self._model

    @property
    def max_unsafe_prob(self):
        return self._max_unsafe_prob

    @property
    def gamma(self):
        return self.γ_c

    def cbf(self, x):
        return (((x[:2] - self.center)**2).sum(-1) - self.radius**2)

    value = cbf

    def grad_cbf(self, x):
        return torch.cat([2 * x[..., :2],
                          x.new_zeros(*x.shape[:-1], 1)], dim=-1)

    def __call__ (self, x, u):
        """
        A(x) u - b(x) < 0
        """
        return self.A(x) @ u - self.b(x)

    def A(self, x):
        return -self.grad_cbf(x) @ self.model.g_func(x)

    def b(self, x):
        return self.grad_cbf(x) @ self.model.f_func(x) + self.gamma * self.cbf(x)


def rotmat(θ):
    θ = θ.unsqueeze(-1).unsqueeze(-1)
    return torch.cat([torch.cat([ θ.cos(), θ.sin()], dim=-1),
                      torch.cat([-θ.sin(), θ.cos()], dim=-1)],
                     dim=-2)


class RelDeg1CLF:
    def __init__(self, model, gamma=2.0, max_unstable_prop=0.01,
                 diagP=[1., 15., 40.], planner=None):
        self._gamma = gamma
        self._model = model
        self._max_unsafe_prob = max_unstable_prop
        self._diagP = torch.tensor(diagP)
        self._planner = planner

    @property
    def model(self):
        return self._model

    @property
    def gamma(self):
        return self._gamma

    @property
    def max_unsafe_prob(self):
        return self._max_unsafe_prob


    def _clf_terms(self, x, x_p, epsilon=1e-6):
        xdiff = x[:2] - x_p[:2]
        ϕ = xdiff[1].atan2(xdiff[0])
        θ = x[2]
        θp = x_p[2]

        e_sq = xdiff @ xdiff
        α = θ-ϕ
        cosα = 1-α.cos()
        return torch.cat([e_sq.unsqueeze(-1), cosα.unsqueeze(-1)])

    def _clf(self, x, x_p):
        return  self._diagP @ self._clf_terms(x, x_p)

    def _grad_clf_x(self, x, x_p):
        with variable_required_grad(x):
            return torch.autograd.grad(self._clf(x, x_p), x, create_graph=True)[0]

    def _grad_clf_x_p(self, x, x_p):
        with variable_required_grad(x_p):
            return torch.autograd.grad(self._clf(x, x_p), x_p, create_graph=True)[0]

    def _dot_clf_gp(self, t, x_p, u0):
        n = self.model.state_size
        grad_V_gp = DeterministicGP(lambda x: - self._grad_clf_x(x, x_p),
                                    shape=(n,),
                                    name="∇ V(x)")
        fu_gp = self.model.fu_func_gp(u0)
        grad_V_gp_xp = DeterministicGP(lambda x: - self._grad_clf_x_p(x, x_p),
                                    shape=(n,),
                                    name="∇_x_p V(x_p)")
        dot_plan = DeterministicGP(lambda x: self._planner.dot_plan(t+1),
                                   shape=(n,),
                                   name="ẋₚ(t)")
        dot_clf_gp = grad_V_gp.t() @ fu_gp + grad_V_gp_xp.t() @ dot_plan
        return dot_clf_gp

    def clc(self, t, u0):
        x_p = self._planner.plan(t+1)
        V_gp = DeterministicGP(lambda x: - self.gamma * self._clf(x, x_p),
                               shape=(1,), name="V(x)")
        return self._dot_clf_gp(t, x_p, u0) + V_gp

    def get_affine_terms(self, x, x_p):
        return (self.model.g_func(x).t() @ self._grad_clf(x, x_p),
                self._grad_clf(x, x_p) @ self.model.f_func(x)
                + self.gamma * self._clf(x, x_p))


class ShiftInvariantModel(ControlAffineRegressor):
    def __init__(self,  *args, invariate_slice=slice(0,2,1), **kw):
        super().__init__(*args, **kw)
        self._invariate_slice = invariate_slice

    def _filter_state(self, Xtest_in):
        ones = Xtest_in.new_ones(Xtest_in.shape[-1])
        ones[self._invariate_slice] = 0
        return Xtest_in * ones

    def f_func_mean(self, Xtest_in):
        return super().f_func_mean(Xtest_in)

    def f_func_knl(self, Xtest_in, Xtestp_in, grad_check=False):
        return super().f_func_knl(self._filter_state(Xtest_in),
                                  self._filter_state(Xtestp_in))

    def fu_func_mean(self, Utest_in, Xtest_in):
        return super().fu_func_mean(Utest_in, self._filter_state(Xtest_in))

    def fu_func_knl(self, Utest_in, Xtest_in, Xtestp_in):
        return super().fu_func_knl(Utest_in, self._filter_state(Xtest_in),
                                   self._filter_state(Xtestp_in))

    def covar_fu_f(self, Utest_in, Xtest_in, Xtestp_in):
        return super().covar_fu_f(Utest_in, self._filter_state(Xtest_in),
                                  self._filter_state(Xtestp_in))


    def f_func(self, Xtest_in):
        return super().f_func(self._filter_state(Xtest_in))

    def g_func(self, Xtest_in):
        return super().g_func(self._filter_state(Xtest_in))


class ControllerUnicycle(ControlCBFLearned):
    ground_truth = False,
    @store_args
    def __init__(self,
                 x_goal=[1, 1, math.pi/4],
                 quad_goal_cost=[[1.0, 0, 0],
                                 [0, 1.0, 0],
                                 [0, 0.0, 0]],
                 u_quad_cost=[[0.1, 0],
                              [0, 1e-4]],
                 egreedy_scheme=[1, 0.1],
                 iterations=100,
                 max_train=200,
                 #gamma_length_scale_prior=[1/deg2rad(0.1), 1],
                 gamma_length_scale_prior=None,
                 true_model=UnicycleDynamicsModel(),
                 plots_dir='data/runs',
                 exp_tags=[],
                 dtype=torch.get_default_dtype(),
                 use_ground_truth_model=False,
                 x_dim=3,
                 u_dim=2,
                 train_every_n_steps=10,
                 dt=0.001,
                 constraint_plotter_class=TensorboardPlotter,
                 cbc_class=ObstacleCBF,
                 obstacles=Obstacle((0, 0), 0.5),
                 numSteps=1000,
                 ctrl_range=[[-10, -10*math.pi],
                             [10, 10*math.pi]],
                 unsafe_controller_class = GreedyController,
                 clf_class=RelDeg1CLF,
                 planner_class=PiecewiseLinearPlanner,
                 summary_writer=None,
                 x0=[-3, 0, math.pi/4],
                 **kwargs
    ):
        if self.use_ground_truth_model:
            model = self.true_model
        else:
            model = ShiftInvariantModel(
                x_dim, u_dim,
                gamma_length_scale_prior=gamma_length_scale_prior)
        cbfs = []
        ground_truth_cbfs = []
        for obs in obstacles:
            cbfs.append( cbc_class(model, obs, dtype=dtype) )
            ground_truth_cbfs.append( cbc_class(true_model, obs,
                                               dtype=dtype) )

        super().__init__(x_dim=x_dim,
                         u_dim=u_dim,
                         train_every_n_steps=train_every_n_steps,
                         dt=dt,
                         constraint_plotter_class=constraint_plotter_class,
                         plots_dir=plots_dir,
                         exp_tags=exp_tags + ['unicycle'],
                         ctrl_range=ctrl_range,
                         x_goal = x_goal,
                         x_quad_goal_cost = quad_goal_cost,
                         u_quad_cost = u_quad_cost,
                         numSteps = numSteps,
                         model = model,
                         unsafe_controller_class=unsafe_controller_class,
                         cbfs=cbfs,
                         ground_truth_cbfs=ground_truth_cbfs,
                         egreedy_scheme=egreedy_scheme,
                         summary_writer=summary_writer,
                         clf_class=clf_class,
                         planner_class=planner_class,
                         x0=x0,
                         **kwargs
        )
        self.x_goal = torch.tensor(x_goal)
        self.x_quad_goal_cost = torch.tensor(quad_goal_cost)

    def control(self, xi, t=None):
        return super().control(xi, t)


class UnicycleVisualizer(Visualizer):
    def __init__(self, centers, radii, x_goal):
        super().__init__()
        self.carworld = CarWithObstacles()
        for c, r in zip(centers, radii):
            self.carworld.addObstacle(c[0], c[1], r)

    def setStateCtrl(self, x, u, t=0, xtp1=None, xtp1_var=None):
        x_ = x[0]
        y_ = x[1]
        theta_ = x[2]
        self.carworld.setCarPose(x_, y_, theta_)
        self.carworld.show()


BBox = namedtuple('BBox', 'XMIN YMIN XMAX YMAX'.split())


class UnicycleVisualizerMatplotlib(Visualizer):
    @store_args
    def __init__(self, robotsize, obstacles, x_goal,
                 summary_writer, every_n_steps=5):
        self.fig, self.axes = plt.subplots(1,1)
        self.summary_writer = summary_writer
        self._bbox = BBox(-2.0, -2.0, 2.0, 2.0)
        self._latest_robot = None
        self._latest_history = None
        self._latest_ellipse = None
        self._history_state = []
        self._history_ctrl = []
        self._init_drawing(self.axes)
        self.every_n_steps = every_n_steps

    def _init_drawing(self, ax):
        self._add_obstacles(ax, self.obstacles)
        if len(self.obstacles):
            obs_bbox = BBox(
                min((c[0]-r) for c, r in self.obstacles),
                min((c[1]-r) for c, r in self.obstacles),
                max((c[0]+r) for c, r in self.obstacles),
                max((c[1]+r) for c, r in self.obstacles))
            self._bbox = BBox(min(obs_bbox.XMIN, self._bbox.XMIN),
                              min(obs_bbox.YMIN, self._bbox.YMIN),
                              max(obs_bbox.XMAX, self._bbox.XMAX),
                              max(obs_bbox.YMAX, self._bbox.YMAX))
        ax.set_xlim(self._bbox.XMIN, self._bbox.XMAX)
        ax.set_ylim(self._bbox.YMIN, self._bbox.YMAX)
        ax.set_aspect('equal')
        self._add_goal(ax, self.x_goal, markersize=1)


    def _add_obstacles(self, ax, obstacles):
        for c, r in obstacles:
            circle = Circle(c, radius=r, fill=True, color='r')
            ax.add_patch(circle)

    def _add_goal(self, ax, pos, markersize, color='g'):
        ax.plot(pos[0], pos[1], '*', markersize=4, color=color)

    def _add_robot(self, ax, pos, theta, robotsize):
        if self._latest_robot is not None:
            self._latest_robot.remove()
        dx = pos[0] + math.cos(theta)* robotsize
        dy = pos[1] + math.sin(theta)* robotsize

        arrow = FancyArrowPatch(to_numpy(pos), (to_numpy(dx), to_numpy(dy)), mutation_scale=10)
        self._latest_robot = ax.add_patch(arrow)
        self._bbox = BBox(min(pos[0], self._bbox.XMIN),
                          min(pos[1], self._bbox.YMIN),
                          max(pos[0], self._bbox.XMAX),
                          max(pos[1], self._bbox.YMAX))
        ax.set_xlim(self._bbox.XMIN, self._bbox.XMAX)
        ax.set_ylim(self._bbox.YMIN, self._bbox.YMAX)

    def _add_history_state(self, ax):
        if self._latest_history is not None:
            for line in self._latest_history:
                line.remove()

        if len(self._history_state):
            hpos = np.asarray(self._history_state)
            self._latest_history = ax.plot(hpos[:, 0], hpos[:, 1], 'b--*')

    def _plot_state_ctrl_history(self, axs):
        hctrl = np.array(self._history_ctrl)
        for i in range(hctrl.shape[-1]):
            ctrlax = axs[i]
            ctrlax.clear()
            ctrlax.set_title("u[{i}]".format(i=i))
            ctrlax.plot(hctrl[:, i])

    def _add_straight_line_path(self, ax, x_goal, x0):
        ax.plot(np.array((x0[0], x_goal[0])),
                np.array((x0[1], x_goal[1])), 'g')


    def _draw_next_step_var(self, ax, xtp1, xtp1_var):
        if self._latest_ellipse is not None:
            for l in self._latest_ellipse:
                l.remove()
        pos = to_numpy(xtp1[:2])
        pos_var = to_numpy(xtp1_var[:2, :2])
        self._latest_ellipse = ax.plot(pos[0], pos[1], 'k.')
        scale, theta = var_to_scale_theta(pos_var)
        scale = np.maximum(scale, self.robotsize / 2)
        self._latest_ellipse.append(
            draw_ellipse(ax, scale, theta, pos)
        )

    def setStateCtrl(self, x, u, t=0, xtp1=None, xtp1_var=None):
        self._add_robot(self.axes, x[:2], x[2], self.robotsize)
        self._add_history_state(self.axes)
        for i in range(u.shape[-1]):
            self.summary_writer.add_scalar('u_%d' % i, u[i], t)
        #self._plot_state_ctrl_history(self.axes2.flatten())
        if not len(self._history_state):
            self._add_straight_line_path(self.axes, self.x_goal, to_numpy(x))
        self._history_state.append(to_numpy(x))
        self._history_ctrl.append(to_numpy(u))

        if xtp1 is not None and xtp1_var is not None:
            self._draw_next_step_var(self.axes, xtp1, xtp1_var)
        #self.fig.savefig(self.plotfile.format(t=t))
        #plt.draw()
        if t % self.every_n_steps == 0:
            img = plot_to_image(self.fig)
            self.summary_writer.add_image('vis', img, t, dataformats='HWC')
            #plt.pause(0.01)


class UnsafeControllerUnicycle(ControllerUnicycle):
    def control(self, xi, t=None):
        ui = self.epsilon_greedy_unsafe_control(t, xi,
                                                min_=self.ctrl_range[0],
                                                max_=self.ctrl_range[1])
        ui = self.unsafe_control(xi, t=t)
        return ui

def run_unicycle_control_learned(
        robotsize=0.2,
        # obstacles=[Obstacle((0.00,  -1.00), 0.8),
        #            Obstacle((0.00,  1.00), 0.8)],
        obstacles=[],
        x0=[-3.0,  -2., np.pi/3.],
        x_goal=[1., 0., np.pi/4.],
        D=200,
        dt=0.002,
        egreedy_scheme=[0.00, 0.00],
        controller_class=partial(ControllerUnicycle,
                                 # mean_dynamics_model_class=partial(
                                 #    ZeroDynamicsModel, m=2, n=3)),
                                 mean_dynamics_model_class=UnicycleDynamicsModel,
                                 enable_learning=False),
        unsafe_controller_class = GreedyController,
        # unsafe_controller_class=ILQR,
        visualizer_class=UnicycleVisualizerMatplotlib,
        summary_writer=None,
        plots_dir='data/runs/',
        exp_tags=[]):
    """
    Run safe unicycle control with learned model
    """
    if summary_writer is None:
        summary_writer = create_summary_writer(
            run_dir=plots_dir,
            exp_tags=exp_tags + [
                'unicycle',
                ('learned' if controller_class.keywords['enable_learning'] else 'fixed'),
                ('true-mean'
                 if issubclass(
                         controller_class.keywords['mean_dynamics_model_class'],
                         UnicycleDynamicsModel)
                 else 'zero-mean')
            ])
    controller = controller_class(
        obstacles=obstacles,
        x_goal=x_goal,
        numSteps=D,
        unsafe_controller_class=unsafe_controller_class,
        dt=dt,
        summary_writer=summary_writer,
        x0=x0)
    return sample_generator_trajectory(
        dynamics_model=UnicycleDynamicsModel(),
        D=D,
        controller=controller.control,
        visualizer=visualizer_class(robotsize, obstacles, x_goal, summary_writer),
        x0=x0,
        dt=dt)

def run_unicycle_control_unsafe():
    run_unicycle_control_learned(
        controller_class=partial(
            UnsafeControllerUnicycle,
            mean_dynamics_model_class=UnicycleDynamicsModel))

def run_unicycle_ilqr(
        robotsize=0.2,
        obstacles=Obstacle(center=(0.10, -0.10), radius=0.5),
        x0=[-0.8, -0.8, 1*math.pi/18],
        x_goal=[1., 1., math.pi/4],
        x_quad_goal_cost=[[1.0, 0, 0],
                          [0, 1.0, 0],
                          [0, 0.0, 1.0]],
        u_quad_cost=[[0.1, 0],
                     [0, 1e-4]],
        D=250,
        dt=0.01,
        ctrl_range=[[-10, -10*math.pi],
                    [10, 10*math.pi]],
        controller_class=partial(ILQR,
                                 model=UnicycleDynamicsModel()),
        visualizer_class=UnicycleVisualizerMatplotlib):
    """
    Run safe unicycle control with learned model
    """
    controller = controller_class(
        Q=torch.tensor(x_quad_goal_cost),
        R=torch.tensor(u_quad_cost),
        x_goal=torch.tensor(x_goal),
        numSteps=D,
        dt=dt,
        ctrl_range=ctrl_range,
        x0=x0)
    return sample_generator_trajectory(
        dynamics_model=UnicycleDynamicsModel(),
        D=D,
        controller=controller.control,
        visualizer=visualizer_class(robotsize, obstacles, x_goal),
        x0=x0,
        dt=dt)


class RRTPlanner:
    def __init__(self, cbfs, x_goal, rng=np.random.default_rng, max_iter=1000,
                 dx=0.1):
        self.cbfs = cbfs
        self.x_goal = x_goal
        self._rng = rng
        self.max_iter= max_iter
        self.dx = dx
        self._space = None
        self._ss = None
        self._make_rrt()

    @staticmethod
    def isStateValid(state):
        # "state" is of type SE2StateInternal, so we don't need to use the "()"
        # operator.
        #
        # Some arbitrary condition on the state (note that thanks to
        # dynamic type checking we can just call getX() and do not need
        # to convert state to an SE2State.)
        x = torch.tensor([state.getX(), state.getY(), state.getTheta()])
        return all((cbf(x) > 0 for cbf in self.cbfs))

    def _make_rrt(self):
        # create an SE2 state space
        self._space = ob.SE2StateSpace()

        # create a simple setup object
        self._ss = og.SimpleSetup(space)
        self._ss.setStateValidityChecker(ob.StateValidityCheckerFn(self.isStateValid))


    def plan(self, x):
        start = ob.State(self._space)
        start().setX(x[0])
        start().setY(x[1])
        start().setYaw(x[2])
        goal = ob.State(self._space)
        goal().setX(self.x_goal[0])
        goal().setY(self.x_goal[1])
        goal().setYaw(self.x_goal[2])
        self._ss.setStartAndGoalStates(start, goal)
        solved = self._ss.solve(1.0)
        self._ss.simplifySolution()
        print(ss.getSolutionPath())


def plan_unicycle_path(x0=[-3.0, 0.1, np.pi/18],
                       cbc_class=ObstacleCBF,
                       obstacles=[Obstacle((0.00,  -1.00), 0.8),
                                  Obstacle((0.00,  1.00), 0.8)],
                       model=UnicycleDynamicsModel(),
                       x_goal=[1.0, 0, np.pi/4],
):
    cbfs = []
    for obs in obstacles:
        cbfs.append( cbc_class(model, obs, dtype=dtype) )
    planner = RRTPlanner(cbfs, x_goal)
    planner.plan(x=x0)


class CosinePlanner(Planner):
    def __init__(self, R, b):
        self.R = R
        self.b = b
        self.n = n

    def plan(self, t):
        R, b, n = map(partial(getattr, self), 'R b n'.split())
        # TODO: where does x,y come from?
        ϕ = y.atan2(x)
        Rpath = R + b.cos(n*ϕ)
        return x - Rpath.cos(ϕ)

    def dot_plan(self, t):
        pass

def run_unicycle_toy_ctrl(robotsize=0.2,
                          D=200,
                          x0=torch.tensor([-3, -1, -np.pi/4]),
                          obstacles=[],
                          x_goal=torch.tensor([0., 0., np.pi/4]),
                          dt=0.01):
    summary_writer = create_summary_writer('data/runs', ['unicycle', 'toy'])
    return sample_generator_trajectory(
        dynamics_model=UnicycleDynamicsModel(),
        D=200,
        # controller=ControllerCLF(
        #     x_goal,
        #     coordinate_converter = lambda x, x_g: x,
        #     dynamics = UnicycleDynamicsModel(),
        #     clf = CLFCartesian(),
        # ).control,
        controller = ControllerUnicycle(
            obstacles=obstacles,
            x_goal=x_goal,
            numSteps=D,
            mean_dynamics_model_class=UnicycleDynamicsModel,
            unsafe_controller_class=GreedyController,
            dt=dt,
            summary_writer=summary_writer,
            enable_learning=False,
            x0=x0).control,
        visualizer=UnicycleVisualizerMatplotlib(
            robotsize, obstacles,
            x_goal,
            summary_writer),
        x0=x0,
        dt=dt)

if __name__ == '__main__':
    import doctest
    doctest.testmod()
    # run_unicycle_control_unsafe()
    # run_unicycle_control_learned()
    # run_unicycle_ilqr()
    # plan_unicycle_path()
    run_unicycle_toy_ctrl()
