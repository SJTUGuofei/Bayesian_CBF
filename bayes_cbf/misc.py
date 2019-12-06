"""
Home for functions/classes that haven't find a home of their own
"""
from functools import wraps, partial
from itertools import zip_longest
from abc import ABC, abstractmethod
import inspect

import torch

t_hstack = partial(torch.cat, dim=-1)
"""
Similar to np.hstack
"""

t_vstack = partial(torch.cat, dim=-2)
"""
Similar to np.vstack
"""


def to_numpy(x):
    return x.detach().cpu().double().numpy()


def t_jac(f_x, x):
    if f_x.ndim:
        return torch.cat(
            [torch.autograd.grad(f_x[i], x, retain_graph=True)[0].unsqueeze(0)
            for i in range(f_x.shape[0])], dim=0)
    else:
        return torch.autograd.grad(f_x, x, retain_graph=True)[0]


def store_args(method):
    argspec = inspect.getfullargspec(method)
    @wraps(method)
    def wrapped_method(self, *args, **kwargs):
        if argspec.defaults is not None:
          for name, val in zip(argspec.args[::-1], argspec.defaults[::-1]):
              setattr(self, name, val)
        if argspec.kwonlydefaults and args.kwonlyargs:
            for name, val in zip(argspec.kwonlyargs, argspec.kwonlydefaults):
                setattr(self, name, val)
        for name, val in zip(argspec.args, args):
            setattr(self, name, val)
        for name, val in kwargs.items():
            setattr(self, name, val)

        method(self, *args, **kwargs)

    return wrapped_method


def torch_kron(A, B):
    """
    >>> B = torch.rand(5,3,3)
    >>> A = torch.rand(5,2,2)
    >>> AB = torch_kron(A, B)
    >>> torch.allclose(AB[1, :3, :3] , A[1, 0,0] * B[1, ...])
    True
    >>> BA = torch_kron(B, A)
    >>> torch.allclose(BA[1, :2, :2] , B[1, 0,0] * A[1, ...])
    True
    """
    b = B.shape[0]
    assert A.shape[0] == b
    B_shape = sum([[1, si] for si in B.shape[1:]], [])
    A_shape = sum([[si, 1] for si in A.shape[1:]], [])
    kron_shape = [a*b for a, b in zip_longest(A.shape[1:], B.shape[1:], fillvalue=1)]
    return (A.reshape(b, *A_shape) * B.reshape(b, *B_shape)).reshape(b, *kron_shape)


class DynamicsModel(ABC):
    """
    Represents mode of the form:

    ẋ = f(x) + g(x)u
    """
    @property
    @abstractmethod
    def ctrl_size(self):
        """
        Dimension of ctrl
        """

    @property
    @abstractmethod
    def state_size(self):
        """
        Dimension of state
        """

    @abstractmethod
    def f_func(self, X):
        """
        ẋ = f(x) + g(x)u

        @param: X : d x self.state_size vector or self.state_size vector
        @returns: f(X)
        """

    @abstractmethod
    def g_func(self, X):
        """
        ẋ = f(x) + g(x)u

        @param: X : d x self.state_size vector or self.state_size vector
        @returns: g(X)
        """


