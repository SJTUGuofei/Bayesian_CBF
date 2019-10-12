from typing import Any
import warnings

import logging
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


import torch


import numpy as np
import torch

from gpytorch.distributions import MultitaskMultivariateNormal, MultivariateNormal, base_distributions
from gpytorch.kernels import ScaleKernel, RBFKernel, WhiteNoiseKernel, IndexKernel
from gpytorch.likelihoods import _GaussianLikelihoodBase,MultitaskGaussianLikelihood, GaussianLikelihood
from gpytorch.likelihoods.noise_models import FixedGaussianNoise
from gpytorch.means import MultitaskMean, ConstantMean
from gpytorch.mlls import ExactMarginalLogLikelihood
from gpytorch.models import ExactGP
import gpytorch.settings as gpsettings

from bayes_cbf.matrix_variate_multitask_kernel import MatrixVariateIndexKernel, HetergeneousMatrixVariateKernel, prod


class Namespace:
    """
    Makes a class as a namespace for static functions
    """
    def __getattribute__(self, name):
        val = object.__getattribute__(self, name)
        if isinstance(val, Callable):
            return staticmethod(val)
        else:
            return val


class Arr(Namespace):
    """
    Namespace for functions that works for both numpy as pytorch
    """
    def cat(arrays, axis=0):
        if isinstance(arrays[0], torch.Tensor):
            X = torch.cat(arrays, dim=axis)
        else:
            X = np.concatenate(arrays, axis=axis)
        return X


class CatEncoder:
    """
    Encodes and decodes the arrays by concatenating them
    """
    def __init__(self, *sizes):
        self.sizes = list(sizes)

    @classmethod
    def from_data(cls, *arrays):
        self = cls(*[A.shape[-1] for A in arrays])
        return self, self.encode(*arrays)

    def encode(self, *arrays):
        X = Arr.cat(arrays, axis=-1)
        return X

    def decode(self, X):
        idxs = np.cumsum([0] + self.sizes)
        arrays = [X[..., s:e]
                  for s,e in zip(idxs[:-1], idxs[1:])]
        return arrays


class IdentityLikelihood(_GaussianLikelihoodBase):
    """
    Dummy likelihood class that does not do anything. It tries to be as close
    to identity as possible.

    gpytorch.likelihoods.Likelihood is supposed to model p(y|f(x)).

    GaussianLikelihood model this by y = f(x) + ε, ε ~ N(0, σ²)

    IdentityLikelihood tries to model y = f(x) , without breaking the gpytorch
    `exact_prediction_strategies` function which requires GaussianLikelihood.
    """
    def __init__(self):
        self.min_possible_noise = 1e-6
        super().__init__(noise_covar=FixedGaussianNoise(noise=torch.tensor(self.min_possible_noise)))

    @property
    def noise(self):
        return 0

    @noise.setter
    def noise(self, _):
        LOG.warn("Ignore setting of noise")

    def forward(self, function_samples: torch.Tensor, *params: Any, **kwargs:
                Any) -> base_distributions.Normal:
        # FIXME: How can we get the covariance of the function samples?
        return base_distributions.Normal(
            function_samples,
            self.min_possible_noise * torch.eye(function_samples.size()))

    def marginal(self, function_dist: MultivariateNormal, *params: Any,
                 **kwargs: Any) -> MultivariateNormal:
        return function_dist


class HetergeneousMatrixVariateMean(MultitaskMean):
    """
    Computes a mean depending on the input.

    Our mean can be the mean of either of the two related GaussianProcesses

        Xdot = F(X)ᵀU

    or

        Y = F(X)ᵀ

    We take input in the form

        M, X, U = MXU

    where M is the mask, where 1 value means we want Xdot = F(X)ᵀU, while 0
    means that we want Y = F(X)ᵀ
    """
    def __init__(self, mean_module, decoder, matshape, **kwargs):
        num_tasks = prod(matshape)
        super().__init__(mean_module, num_tasks, **kwargs)
        self.decoder = decoder
        self.matshape = matshape

    def forward(self, MXU):
        B = MXU.shape[:-1]

        Ms, X, UH = self.decoder.decode(MXU)
        assert Ms.size(-1) == 1
        Ms = Ms[..., 0]
        idxs = torch.nonzero(Ms - Ms.new_ones(Ms.size()))
        idxend = torch.min(idxs) if idxs.numel() else Ms.size(-1)
        # assume sorted
        assert (Ms[..., idxend:] == 0).all()
        UH = UH[..., :idxend, :]
        X1 = X[..., :idxend, :]
        X2 = X[..., idxend:, :]
        mu = torch.cat([sub_mean(MXU).unsqueeze(-1)
                        for sub_mean in self.base_means], dim=-1)
        mu  = mu.reshape(-1, *self.matshape)
        XdotMean = UH.unsqueeze(-2) @ mu[:idxend, ...] # D x n
        output = XdotMean.reshape(-1)
        if Ms.size(-1) != idxend:
            Fmean = mu[idxend:, ...].reshape(-1)
            output = torch.cat([output, Fmean])
        return output


class DynamicsModelExactGP(ExactGP):
    """
    ExactGP Model to capture the heterogeneous gaussian process

    Given MXU, M, X, U = MXU

        Xdot = F(X)ᵀU    if M = 1
        Y = F(X)ᵀ        if M = 0
    """
    def __init__(self, Xtrain, Utrain, XdotTrain, likelihood, rank=1):
        self.matshape = (1+Utrain.size(-1), Xtrain.size(-1))
        self.decoder, MXUtrain = self.encode_from_XU(Xtrain, Utrain, 1)
        super(DynamicsModelExactGP, self).__init__(MXUtrain, XdotTrain.reshape(-1),
                                              likelihood)
        self.mean_module = HetergeneousMatrixVariateMean(
            ConstantMean(),
            self.decoder,
            self.matshape)

        task_covar = MatrixVariateIndexKernel(
                IndexKernel(num_tasks=self.matshape[1]),
                IndexKernel(num_tasks=self.matshape[0]),
            )
        input_covar = ScaleKernel(RBFKernel())
        self.covar_module = HetergeneousMatrixVariateKernel(
            task_covar,
            input_covar,
            self.decoder)

    def encode_from_XU(self, Xtrain, Utrain=None, M=0):
        Mtrain = Xtrain.new_full([Xtrain.size(0), 1], M)
        if M:
            assert Utrain is not None
            UHtrain = torch.cat([Mtrain, Utrain], dim=1)
        else:
            UHtrain = Xtrain.new_zeros((Xtrain.size(0), self.matshape[0]))
        return CatEncoder.from_data(Mtrain, Xtrain, UHtrain)

    def forward(self, mxu):
        mean_x = self.mean_module(mxu)
        with gpsettings.lazily_evaluate_kernels(False):
            covar_x = self.covar_module(mxu)
        return MultivariateNormal(mean_x, covar_x)


def default_device():
    return 'cuda' if torch.cuda.is_available() else 'cpu'


class DynamicModelGP:
    """
    Scikit like wrapper around learning and predicting GaussianProcessRegressor

    Usage:
    F(X), COV(F(X)) = DynamicModelGP()
                        .fit(Xtrain, Utrain, XdotTrain)
                        .predict(Xtest, return_cov=True)
    """
    def __init__(self, device=None, default_device=default_device):
        self.likelihood = None
        self.model = None
        self.device = device or default_device()

    def fit(self, *args, max_cg_iterations=2000, **kwargs):
        with warnings.catch_warnings(), \
              gpsettings.max_cg_iterations(max_cg_iterations):
            warnings.simplefilter("ignore")
            return self._fit_with_warnings(*args, **kwargs)

    def _fit_with_warnings(self, Xtrain, Utrain, XdotTrain, training_iter = 50,
                           lr=0.1):
        # Convert to torch
        device = self.device
        Xtrain = torch.from_numpy(Xtrain).float().to(device=device)
        Utrain = torch.from_numpy(Utrain).float().to(device=device)
        XdotTrain = torch.from_numpy(XdotTrain).float().to(device=device)

        # Initialize model and likelihood
        # Noise model for GPs
        likelihood = self.likelihood = IdentityLikelihood()
        # Actual model
        model = self.model = DynamicsModelExactGP(Xtrain, Utrain,
                                                  XdotTrain,
                                                  likelihood).to(device=device)

        # Find optimal model hyperparameters
        model.train()
        likelihood.train()

        # Use the adam optimizer
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)

        # "Loss" for GPs - the marginal log likelihood
        # num_data refers to the amount of training data
        # mll = VariationalELBO(likelihood, model, Y.numel())
        mll = ExactMarginalLogLikelihood(likelihood, model)
        for i in range(training_iter):
            # Zero backpropped gradients from previous iteration
            optimizer.zero_grad()
            # Get predictive output
            output = model(*model.train_inputs)
            # Calc loss and backprop gradients
            loss = -mll(output, XdotTrain.reshape(-1))
            loss.backward()
            print('Iter %d/%d - Loss: %.3f' % (i + 1, training_iter, loss.item()))
            optimizer.step()
        return self

    def predict(self, Xtest, return_cov=True):
        device = self.device
        Xtest = torch.from_numpy(Xtest).float().to(device=device)
        # Switch back to eval mode
        if self.model is None or self.likelihood is None:
            raise RuntimeError("Call fit() with training data before calling predict")

        self.model.eval()
        self.likelihood.eval()

        # Concatenate the test set
        _, MXUHtest = self.model.encode_from_XU(Xtest)
        output = self.model(MXUHtest)

        mean, cov = (output.mean.reshape(-1, *self.model.matshape),
                     output.covariance_matrix)
        mean_np, cov_np = [arr.detach().cpu().numpy() for arr in (mean, cov)]
        return (mean_np, cov_np) if return_cov else mean

