#plot the result
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import rc as mplibrc
mplibrc('text', usetex=True)

def rad2deg(rad):
    return rad / np.pi * 180


def plot_2D_f_func(f_func,
                   axes_gen = lambda FX: plt.subplots(1, FX.shape[-1])[1],
                   theta_range = slice(-np.pi, np.pi, np.pi/20),
                   omega_range = slice(-np.pi, np.pi,np.pi/20),
                   axtitle="f(x)[{i}]"):
    # Plot true f(x)
    theta_omega_grid = np.mgrid[theta_range, omega_range]
    D, N, M = theta_omega_grid.shape
    FX = f_func(theta_omega_grid.transpose(1, 2, 0).reshape(-1, D)).reshape(N, M, D)
    axs = axes_gen(FX)
    for i in range(FX.shape[-1]):
        ctf0 = axs[i].contourf(theta_omega_grid[0, ...], theta_omega_grid[1, ...],
                               FX[:, :, i])
        plt.colorbar(ctf0, ax=axs[i])
        axs[i].set_title(axtitle.format(i=i))
        axs[i].set_ylabel(r"$\omega$")
        axs[i].set_xlabel(r"$\theta$")


def plot_results(time_vec, omega_vec, theta_vec, u_vec,
                 axs=None):
    #plot thetha
    if axs is None:
        fig, axs = plt.subplots(2,2)
    axs[0,0].clear()
    axs[0,0].plot(time_vec, rad2deg((theta_vec + np.pi) % (2*np.pi) - np.pi),
                  ":", label = "theta (degrees)",color="blue")
    axs[0,0].set_ylabel("theta (degrees)")

    axs[0,1].clear()
    axs[0,1].plot(time_vec, omega_vec,":", label = "omega (rad/s)",color="blue")
    axs[0,1].set_ylabel("omega")

    axs[1,0].clear()
    axs[1,0].plot(time_vec, u_vec,":", label = "u",color="blue")
    axs[1,0].set_ylabel("u")

    axs[1,1].clear()
    axs[1,1].plot(time_vec, np.cos(theta_vec),":", label="cos(theta)", color="blue")
    axs[1,1].set_ylabel("cos/sin(theta)")
    axs[1,1].plot(time_vec, np.sin(theta_vec),":", label="sin(theta)", color="red")
    axs[1,1].set_ylabel("sin(theta)")
    axs[1,1].legend()

    fig = axs[0, 0].figure
    fig.suptitle("Pendulum")
    fig.subplots_adjust(wspace=0.31)
    return axs


def plot_learned_2D_func(Xtrain, learned_f_func, true_f_func,
                         axtitle="f(x)[{i}]"):
    fig, axs = plt.subplots(3,2)
    theta_range = slice(np.min(Xtrain[:, 0]), np.max(Xtrain[:, 0]),
                        (np.max(Xtrain[:, 0]) - np.min(Xtrain[:, 0])) / 20)
    omega_range = slice(np.min(Xtrain[:, 1]), np.max(Xtrain[:, 1]),
                      (np.max(Xtrain[:, 1]) - np.min(Xtrain[:, 1])) / 20)
    plot_2D_f_func(true_f_func, axes_gen=lambda _: axs[0, :],
                   theta_range=theta_range, omega_range=omega_range,
                   axtitle="True " + axtitle)
    plot_2D_f_func(learned_f_func, axes_gen=lambda _: axs[1, :],
                   theta_range=theta_range, omega_range=omega_range,
                   axtitle="Learned " + axtitle)
    ax = axs[2,0]
    ax.plot(Xtrain[:, 0], Xtrain[:, 1], marker='*', linestyle='')
    ax.set_ylabel(r"$\omega$")
    ax.set_xlabel(r"$\theta$")
    ax.set_xlim(theta_range.start, theta_range.stop)
    ax.set_ylim(omega_range.start, omega_range.stop)
    ax.set_title("Training data")
    fig.subplots_adjust(wspace=0.3,hspace=0.8)
    return fig

class LinePlotSerialization:
    @staticmethod
    def serialize(filename, axes):
        xydata = {
            "ax_{i}_line_{j}_{xy}".format(i=i,j=j,xy=xy): method()
            for xy, method in (("x", ax.get_xdata), ("y",ax.get_ydata))
            for j in ax.lines
            for i, ax in enumerate(axes)
        }
        np.savez_compressed(
            filename,
            **xydata
        )

    @staticmethod
    def example_plot(ax_lines_xydata):
        for i, ax in ax_lines_xydata.items():
            for j, xydata in ax_lines_xydata.items():
                axes[i].plot(xydata["x"], xydata["y"])

    @staticmethod
    def deserialize(filename, axes):
        xydata = np.loadz(filename)
        ax_lines_xydata = {}
        for key, val in xydata.items():
            _, istr,_, jstr, xy = key.split("_")
            i, j = int(istr), int(jstr)
            ax_lines_xydata.setdefault(i, {}).setdefault(j, {})[xy] = val
        return ax_lines_xydata


def plt_savefig_with_data(fig, filename):
    npz_filename = osp.splitext(filename)[0] + ".npz"
    LinePlotSerialization.serialize(npz_filename, fig.get_axes())
    fig.savefig(filename)
