#!/usr/bin/env python3
import operator
from functools import reduce

import torch

from gpytorch.kernels import MultitaskKernel, IndexKernel, Kernel
from gpytorch.lazy import (KroneckerProductLazyTensor, BlockDiagLazyTensor,
                           InterpolatedLazyTensor, lazify, NonLazyTensor,
                           LazyTensor, cat as lazycat)
import gpytorch.settings as gpsettings


def prod(L):
    return reduce(operator.mul, L, 1)


class MatrixVariateIndexKernel(Kernel):
    """
    Wraps IndexKernel to represent
    https://en.wikipedia.org/wiki/Matrix_normal_distribution

    P(X | M, U, V) = exp(-0.5 tr[ V⁻¹ (X - M)ᵀ U⁻¹ (X-M) ] ) / √((2π)ⁿᵖ|V|ⁿ|U|ᵖ)

    vec(X) ~ 𝒩(M, V ⊗ U)

    This kernel represents the covariance_matrix V ⊗ U given V and U.
    """
    def __init__(self, U : IndexKernel, V: IndexKernel):
        super(MatrixVariateIndexKernel, self).__init__()
        self.U = U
        self.V = V
        n = self.U.raw_var.shape[-1]
        p = self.V.raw_var.shape[-1]
        self.matshape = (n,p)

    @property
    def covar_matrix(self):
        U = self.U.covar_matrix
        V = self.V.covar_matrix
        return KroneckerProductLazyTensor(V, U)

    def forward(self, i1, i2, **params):
        assert i1.dtype in (torch.int64, torch.int32)
        assert i2.dtype in (torch.int64, torch.int32)
        covar_matrix = self.covar_matrix
        res = InterpolatedLazyTensor(base_lazy_tensor=covar_matrix,
                                     left_interp_indices=i1, right_interp_indices=i2)
        return res


def test_MatrixVariateIndexKernel(n=2, m=3):
    mvk = MatrixVariateIndexKernel(IndexKernel(n), IndexKernel(m))
    x = torch.ranint(n*m, size=(10,))
    mvk(x,x).evaluate()


def ensurelazy(X):
    return X if isinstance(X, LazyTensor) else NonLazyTensor(X)


class MatrixVariateKernel(Kernel):
    """
    Kernel supporting Kronecker style matrix variate Gaussian processes (where every
    data point is evaluated at every task).

    Given a base covariance module to be used for the data, :math:`K_{XX}`,
    this kernel computes a task kernel of specified size :math:`K_{TT}` and
    returns :math:`K = K_{TT} \otimes K_{XX}`. as an
    :obj:`gpytorch.lazy.KroneckerProductLazyTensor`.

    Args:
        task_covar_module (:obj:`gpytorch.kernels.IndexKernel`):
            Kernel to use as the task kernel
        data_covar_module (:obj:`gpytorch.kernels.Kernel`):
            Kernel to use as the data kernel.
        num_tasks (int):
            Number of tasks
        batch_size (int, optional):
            Set if the MultitaskKernel is operating on batches of data (and you
            want different parameters for each batch)
        rank (int):
            Rank of index kernel to use for task covariance matrix.
        task_covar_prior (:obj:`gpytorch.priors.Prior`):
            Prior to use for task kernel. See :class:`gpytorch.kernels.IndexKernel` for details.
    """
    @property
    def num_tasks(self):
        return prod(self.task_covar_module.matshape)

    def __init__(self, task_covar_module, data_covar_module, decoder, **kwargs):
        """
        """
        super().__init__(data_covar_module, **kwargs)
        self.task_covar_module = task_covar_module
        self.data_covar_module = data_covar_module
        self.decoder = decoder


class HetergeneousMatrixVariateKernel(MatrixVariateKernel):
    def num_outputs_per_input(self, mxu1, mxu2):
        M1, X1, U1 = self.decoder.decode(mxu1)
        M2, X2, U2 = self.decoder.decode(mxu2)

        M1s = M1[..., 0]
        idxs1 = torch.nonzero(M1s - torch.ones_like(M1s))
        idxend1 = torch.min(idxs1).item() if idxs1.numel() else M1s.size(-1)

        return (idxend1 * X1.shape[-1] + (M1s.size(-1) - idxend1) * prod(
            self.task_covar_module.matshape) ) // M1s.size(-1)

    def mask_dependent_covar(self, M1s, U1, M2s, U2, covar_xx):
        # Assume M1s, M2s sorted descending
        B = M1s.shape[:-1]
        M1s = M1s[..., 0]
        idxs1 = torch.nonzero(M1s - torch.ones_like(M1s))
        idxend1 = torch.min(idxs1).item() if idxs1.numel() else M1s.size(-1)
        # assume sorted
        assert (M1s[..., idxend1:] == 0).all()
        U1s = U1[..., :idxend1, :]

        M2s = M2s[..., 0]
        idxs2 = torch.nonzero(M2s - torch.ones_like(M2s))
        idxend2 = torch.min(idxs2).item() if idxs2.numel() else M2s.size(-1)
        # assume sorted
        assert (M2s[..., idxend2:] == 0).all()
        U2s = U2[..., :idxend2, :]

        H1 = BlockDiagLazyTensor(NonLazyTensor(U1s.unsqueeze(1)))
        #if gpsettings.debug.on(): H1.evaluate()
        H2 = BlockDiagLazyTensor(NonLazyTensor(U2s.unsqueeze(1)))
        #if gpsettings.debug.on(): H2.evaluate()

        Kxx = ensurelazy(covar_xx)
        # If M1, M2 = (1, 1)
        #    H₁ᵀ [ K ⊗ B ] H₂ ⊗ A
        V = ensurelazy(self.task_covar_module.V.covar_matrix)
        U = ensurelazy(self.task_covar_module.U.covar_matrix)
        Kij_xx_11 = KroneckerProductLazyTensor(
            H1 @ KroneckerProductLazyTensor(Kxx[:idxend1, :idxend2], V) @ H2.t(), U)
        #Kij_xx_11.evaluate()

        if idxend1 < M1s.size(-1):
            # elif M1, M2 = (1, 0)
            #    H₁ᵀ [ k_x* ⊗ B ] ⊗ A
            k_xx_12 = Kxx[:idxend1, idxend2:]
            Kij_xx_12 = KroneckerProductLazyTensor(
                H1 @ KroneckerProductLazyTensor(k_xx_12, V) , U)
            #Kij_xx_12.evaluate()

            # elif M1, M2 = (0, 1)
            #    [ k_x* ⊗ B ] H₂ ⊗ A
            Kij_xx_21 = Kij_xx_12.t()
            # else M1, M2 = (0, 0)
            #    [ k_** ⊗ B ] ⊗ A
            k_xx_22 = Kxx[idxend1:, idxend2:]
            Kij_xx_22 = KroneckerProductLazyTensor(
                KroneckerProductLazyTensor(k_xx_22, V), U)
            #Kij_xx_22.evaluate()
            out = lazycat([lazycat([Kij_xx_11, Kij_xx_12], dim=1),
                            lazycat([Kij_xx_21, Kij_xx_22], dim=1)], dim=0)
            #out.evaluate()
            return out

        return Kij_xx_11


    def forward(self, mxu1, mxu2, diag=False, last_dim_is_batch=False, **params):
        M1, X1, U1 = self.decoder.decode(mxu1)
        M2, X2, U2 = self.decoder.decode(mxu2)

        if last_dim_is_batch:
            raise RuntimeError("MultitaskKernel does not accept the last_dim_is_batch argument.")
        #covar_i = self.mask_dependent_covar(self, M1, U1, M2, U2)
        covar_x = lazify(self.data_covar_module.forward(X1, X2, **params))
        res = self.mask_dependent_covar(M1, U1, M2, U2, covar_x)
        return res.diag() if diag else res
