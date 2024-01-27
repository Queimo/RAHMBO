import numpy as np
import torch

tkwargs = {
    "dtype": torch.double,
    "device": torch.device("cuda" if torch.cuda.is_available() else "cpu"),
}

from botorch.models.gp_regression import (
    FixedNoiseGP,
    HeteroskedasticSingleTaskGP,
    SingleTaskGP,
)
from botorch.models.model_list_gp_regression import ModelListGP
from botorch.models.transforms.outcome import Standardize
from botorch.models.transforms.input import InputPerturbation
from gpytorch.mlls.sum_marginal_log_likelihood import SumMarginalLogLikelihood
from gpytorch.mlls.exact_marginal_log_likelihood import ExactMarginalLogLikelihood
from gpytorch.mlls.predictive_log_likelihood import PredictiveLogLikelihood
from botorch.fit import fit_gpytorch_mll, fit_gpytorch_model, fit_gpytorch_mll_torch

import botorch
from botorch.acquisition.objective import ExpectationPosteriorTransform

from mobo.surrogate_model.botorch_gp_wrapper import BoTorchSurrogateModel
from mobo.utils import safe_divide

from linear_operator.settings import _fast_solves


class BoTorchSurrogateModelReapeat(BoTorchSurrogateModel):

    """
    Gaussian process
    """

    def __init__(self, n_var, n_obj, **kwargs):
        super().__init__(n_var, n_obj)

    def initialize_model(self, train_x, train_y, train_rho=None):
        # define models for objective and constraint
        train_y_mean = -train_y  # negative because botorch assumes maximization
        # train_y_var = self.real_problem.evaluate(train_x).to(**tkwargs).var(dim=-1)
        train_y_var = train_rho + 1e-6
        model = HeteroskedasticSingleTaskGP(
            train_X=train_x,
            train_Y=train_y_mean,
            train_Yvar=train_y_var,
            #identity input transform to expand
            input_transform=InputPerturbation(torch.zeros((11, self.n_var), **tkwargs)),
            outcome_transform=Standardize(m=self.n_obj),
        )
        mll = ExactMarginalLogLikelihood(model.likelihood, model)
        return mll, model

    def evaluate(self, X, std=False, noise=False, calc_gradient=False, calc_hessian=False,):
        X = torch.tensor(X).to(**tkwargs)

        F, dF, hF = None, None, None  # mean
        S, dS, hS = None, None, None  # std
        rho_F, drho_F = None, None  # noise mean
        rho_S, drho_S = None, None  # noise std 
        
        post = self.bo_model.posterior(X, posterior_transform=ExpectationPosteriorTransform(n_w=11))
        # negative because botorch assumes maximization (undo previous negative)
        F = -post.mean.squeeze(-1).detach().cpu().numpy()
        S = post.variance.sqrt().squeeze(-1).detach().cpu().numpy()

        if noise:
            rho_post = self.bo_model.likelihood.noise_covar.noise_model.posterior(X)
            rho_F = rho_post.mean.detach().cpu().numpy()[::11,:]
            rho_S = rho_post.variance.sqrt().detach().cpu().numpy()[::11,:]
            if calc_gradient:
                jac_rho = torch.autograd.functional.jacobian(
                    lambda x: self.bo_model.likelihood.noise_covar.noise_model.posterior(x).mean.T, X
                )
                drho_F = (
                    jac_rho.diagonal(dim1=0, dim2=2)
                    .transpose(0, -1)
                    .transpose(1, 2)
                    .detach()
                    .numpy()
                )
                if std:
                    jac_rho = torch.autograd.functional.jacobian(
                        lambda x: self.bo_model.likelihood.noise_covar.noise_model.posterior(x, posterior_transform=ExpectationPosteriorTransform(n_w=11)).variance.sqrt().T, X
                    )
                    drho_S = (
                        jac_rho.diagonal(dim1=0, dim2=2)
                        .transpose(0, -1)
                        .transpose(1, 2)
                        .detach()
                        .numpy()
                    )

        # #simplest 2d --> 2d test problem
        # def f(X):
        #     return torch.stack([X[:, 0]**2 + 0.1*X[:, 1]**2 , -X[:, 1]**2 -0.1*(X[:, 0]**2)]).T
        # X_toy = torch.tensor([[0.5, 0.5], [1., 1.], [2., 2.]], requires_grad=True)
        # jacobian_mean = torch.autograd.functional.jacobian(f, X_toy)
        # # goal 3 x 2 x 2
        # jac_batch = jacobian_mean.diagonal(dim1=0,dim2=2).transpose(0,-1).transpose(1,2).numpy()

        if calc_gradient:
            jac_F = torch.autograd.functional.jacobian(
                lambda x: -self.bo_model.posterior(x, posterior_transform=ExpectationPosteriorTransform(n_w=11)).mean.T, X
            )
            dF = (
                jac_F.diagonal(dim1=0, dim2=2)
                .transpose(0, -1)
                .transpose(1, 2)
                .detach()
                .numpy()
            )

            if std:
                jac_S = torch.autograd.functional.jacobian(
                    lambda x: self.bo_model.posterior(x).variance.sqrt().T, X
                )
                dS = (
                    jac_S.diagonal(dim1=0, dim2=2)
                    .transpose(0, -1)
                    .transpose(1, 2)
                    .detach()
                    .numpy()
                )

        out = {
            "F": F,
            "dF": dF,
            "hF": hF,
            "S": S,
            "dS": dS,
            "hS": hS,
            "rho_F": rho_F,
            "drho_F": drho_F,
            "rho_S": rho_S,
            "drho_S": drho_S,
        }
         
        return out