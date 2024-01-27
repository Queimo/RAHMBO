from . import NSGA2Solver, Solver
from pymoo.algorithms.nsga2 import NSGA2


import numpy as np
import torch

from botorch.optim.optimize import optimize_acqf
from botorch.acquisition.multi_objective.monte_carlo import (
    qExpectedHypervolumeImprovement,
    qNoisyExpectedHypervolumeImprovement,
)

from botorch.acquisition.multi_objective.logei import qLogExpectedHypervolumeImprovement, qLogNoisyExpectedHypervolumeImprovement
from pymoo.util.nds.non_dominated_sorting import NonDominatedSorting
from botorch.utils.multi_objective.box_decompositions.non_dominated import (
    FastNondominatedPartitioning,
)

from botorch.acquisition.multi_objective.objective import MCMultiOutputObjective

import os

tkwargs = {
    "dtype": torch.double,
    "device": torch.device("cuda" if torch.cuda.is_available() else "cpu"),
}
SMOKE_TEST = os.environ.get("SMOKE_TEST")
import warnings

from botorch.exceptions import BadInitialCandidatesWarning
from botorch.sampling.normal import SobolQMCNormalSampler


from botorch.utils.transforms import (
    concatenate_pending_points,
    is_ensemble,
    match_batch_shape,
    t_batch_mode_transform,
    standardize
)
warnings.filterwarnings("ignore", category=BadInitialCandidatesWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

NUM_RESTARTS = 10 if not SMOKE_TEST else 2
RAW_SAMPLES = 512 if not SMOKE_TEST else 4
MC_SAMPLES = 128 if not SMOKE_TEST else 16

class qLogNEHVI(qNoisyExpectedHypervolumeImprovement):
    
    @concatenate_pending_points
    @t_batch_mode_transform()
    def forward(self, X): 
        X_full = torch.cat([match_batch_shape(self.X_baseline, X), X], dim=-2)
        X_full_repeated = X_full.expand([-1, X_full.shape[-2]*10, -1])
        posterior = self.model.posterior(X_full, observation_noise=True)
        event_shape_lag = 1 if is_ensemble(self.model) else 2
        n_w = (
            posterior._extended_shape()[X_full.dim() - event_shape_lag]
            // X_full.shape[-2]
        )
        q_in = X.shape[-2] * n_w
        self._set_sampler(q_in=q_in, posterior=posterior)
        samples = self._get_f_X_samples(posterior=posterior, q_in=q_in)
        # Add previous nehvi from pending points.
        return self._compute_qehvi(samples=samples, X=X) + self._prev_nehvi
    
class MeanVarianceObjective(MCMultiOutputObjective):
    def __init__(self, beta):
        super().__init__()
        self.beta = beta

    def forward(self, samples, X=None, n_w=11):
        mean = samples.mean(dim=0)
        std = samples.std(dim=0)
        return samples.view(samples.shape[0], 11, X.shape[0], -1).mean(dim=-3, keepdim=True)
        return mean - self.beta * std

beta = 0.5  # Adjust this based on your risk preference
objective = MeanVarianceObjective(beta)

class RAqNEHVISolver(NSGA2Solver):
    '''
    Solver based on PSL
    '''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        

    # def solve(self, problem, X, Y, rho):
    #     standard_bounds = torch.zeros(2, problem.n_var, **tkwargs)
    #     standard_bounds[1] = 1
    #     surrogate_model = problem.surrogate_model
        
    #     ref_point = self.ref_point
    #     print("ref_point", ref_point)
        
    #     # use nsga2 to find pareto front for later plots, has limitations
    #     self.solution = super().solve(problem, X, Y) 
        
    #     sampler = SobolQMCNormalSampler(sample_shape=torch.Size([MC_SAMPLES]))
    #     # solve surrogate problem
    #     # define acquisition functions
        
    #     acq_func = qLogNoisyExpectedHypervolumeImprovement(
    #         model=surrogate_model.bo_model,
    #         ref_point=ref_point,  # use known reference point
    #         X_baseline=torch.from_numpy(X).to(**tkwargs),
    #         prune_baseline=True,  # prune baseline points that have estimated zero probability of being Pareto optimal
    #         sampler=sampler,
    #     )
        
    #     options = {"batch_limit": self.batch_size, "maxiter": 2000}
        
    #     while options["batch_limit"] >= 1:
    #         try:
    #             torch.cuda.empty_cache()
    #             X_cand, Y_cand_pred = optimize_acqf(
    #                 acq_function=acq_func,
    #                 bounds=standard_bounds,
    #                 q=self.batch_size,
    #                 num_restarts=NUM_RESTARTS,
    #                 raw_samples=RAW_SAMPLES,  # used for intialization heuristic
    #                 options=options,
    #                 sequential=True,
    #             )
    #             torch.cuda.empty_cache()
    #             break
    #         except RuntimeError as e:
    #             if options["batch_limit"] > 1:
    #                 print(
    #                     "Got a RuntimeError in `optimize_acqf`. "
    #                     "Trying with reduced `batch_limit`."
    #                 )
    #                 options["batch_limit"] //= 2
    #                 continue
    #             else:
    #                 raise e 
        
    #     selection = {'x': np.array(X_cand), 'y': np.array(Y_cand_pred)}
        
    #     return selection

from botorch.acquisition.multi_objective.multi_output_risk_measures import MARS
from botorch.acquisition.monte_carlo import qNoisyExpectedImprovement
from botorch.utils.sampling import sample_simplex

class qNEI(
    qNoisyExpectedImprovement
):
    def _get_samples_and_objectives(self, X):
        r"""Compute samples at new points, using the cached root decomposition.

        Args:
            X: A `batch_shape x q x d`-dim tensor of inputs.

        Returns:
            A two-tuple `(samples, obj)`, where `samples` is a tensor of posterior
            samples with shape `sample_shape x batch_shape x q x m`, and `obj` is a
            tensor of MC objective values with shape `sample_shape x batch_shape x q`.
        """
        q = X.shape[-2]
        X_full = torch.cat([match_batch_shape(self.X_baseline, X), X], dim=-2)
        # TODO: Implement more efficient way to compute posterior over both training and
        # test points in GPyTorch (https://github.com/cornellius-gp/gpytorch/issues/567)
        posterior = self.model.posterior(
            X_full, posterior_transform=self.posterior_transform, observation_noise=True
        )
        self._cache_root = False
        if not self._cache_root:
            samples_full = super().get_posterior_samples(posterior)
            samples = samples_full[..., -q:, :]
            obj_full = self.objective(samples_full, X=X_full)
            # assigning baseline buffers so `best_f` can be computed in _sample_forward
            self.baseline_obj, obj = obj_full[..., :-q], obj_full[..., -q:]
            self.baseline_samples = samples_full[..., :-q, :]
        else:
            # handle one-to-many input transforms
            n_plus_q = X_full.shape[-2]
            n_w = posterior._extended_shape()[-2] // n_plus_q
            q_in = q * n_w
            self._set_sampler(q_in=q_in, posterior=posterior)
            samples = self._get_f_X_samples(posterior=posterior, q_in=q_in)
            obj = self.objective(samples, X=X_full[..., -q:, :])


        return samples, obj

def get_MARS_NEI(
    model,
    n_w,
    X_baseline,
    sampler,
    mvar_ref_point,
):
    r"""Construct the NEI acquisition function with VaR of Chebyshev scalarizations.
    Args:
        model: A fitted multi-output GPyTorchModel.
        n_w: the number of perturbation samples
        X_baseline: An `r x d`-dim tensor of points already observed.
        sampler: The sampler used to draw the base samples.
        mvar_ref_point: The mvar reference point.
    Returns:
        The NEI acquisition function.
    """
    # sample weights from the simplex
    weights = sample_simplex(
        d=mvar_ref_point.shape[0],
        n=1,
        dtype=X_baseline.dtype,
        device=X_baseline.device,
    ).squeeze(0)
    # set up mars objective
    mars = MARS(
        alpha=0.9,
        n_w=n_w,
        chebyshev_weights=weights,
        ref_point=mvar_ref_point,
    )
    # set normalization bounds for the scalarization
    mars.set_baseline_Y(model=model, X_baseline=X_baseline)
    # initial qNEI acquisition function with the MARS objective
    acq_func = qNEI(
        model=model,
        X_baseline=X_baseline,
        objective=mars,
        prune_baseline=True,
        sampler=sampler,
    )
    return acq_func

class MARSSolver(NSGA2Solver):
    '''
    Solver based on PSL
    '''
    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)
        

    def solve(self, problem, X, Y, rho):
        standard_bounds = torch.zeros(2, problem.n_var, **tkwargs)
        standard_bounds[1] = 1
        surrogate_model = problem.surrogate_model
        
        ref_point = self.ref_point
        print("ref_point", ref_point)
        
        # use nsga2 to find pareto front for later plots, has limitations
        self.solution = super().solve(problem, X, Y) 
        
        sampler = SobolQMCNormalSampler(sample_shape=torch.Size([MC_SAMPLES]))
        
        acq_func = get_MARS_NEI(
            model=surrogate_model.bo_model,
            n_w=11,
            X_baseline=torch.from_numpy(X).to(**tkwargs),
            sampler=sampler,
            mvar_ref_point=torch.tensor(ref_point),
        )
        
        options = {"batch_limit": self.batch_size, "maxiter": 2000}
        
        while options["batch_limit"] >= 1:
            try:
                torch.cuda.empty_cache()
                X_cand, Y_cand_pred = optimize_acqf(
                    acq_function=acq_func,
                    bounds=standard_bounds,
                    q=self.batch_size,
                    num_restarts=NUM_RESTARTS,
                    raw_samples=RAW_SAMPLES,  # used for intialization heuristic
                    options=options,
                    sequential=True,
                )
                torch.cuda.empty_cache()
                break
            except RuntimeError as e:
                if options["batch_limit"] > 1:
                    print(
                        "Got a RuntimeError in `optimize_acqf`. "
                        "Trying with reduced `batch_limit`."
                    )
                    options["batch_limit"] //= 2
                    continue
                else:
                    raise e 
        
        selection = {'x': np.array(X_cand), 'y': np.array(Y_cand_pred)}
        
        # self.solution = {'x': np.array(X), 'y': np.array(Y)}
        return selection
class qNEHVISolver(NSGA2Solver):
    '''
    Solver based on PSL
    '''
    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)
        

    def solve(self, problem, X, Y, rho):
        standard_bounds = torch.zeros(2, problem.n_var, **tkwargs)
        standard_bounds[1] = 1
        surrogate_model = problem.surrogate_model
        
        ref_point = self.ref_point
        print("ref_point", ref_point)
        
        # use nsga2 to find pareto front for later plots, has limitations
        self.solution = super().solve(problem, X, Y) 
        
        sampler = SobolQMCNormalSampler(sample_shape=torch.Size([MC_SAMPLES]))
        # solve surrogate problem
        # define acquisition functions
        acq_func = qLogNoisyExpectedHypervolumeImprovement(
        # acq_func = qLogNEHVI(
            model=surrogate_model.bo_model,
            ref_point=ref_point,  # use known reference point
            X_baseline=torch.from_numpy(X).to(**tkwargs),
            prune_baseline=True,  # prune baseline points that have estimated zero probability of being Pareto optimal
            sampler=sampler,
        )
        
        options = {"batch_limit": self.batch_size, "maxiter": 2000}
        
        while options["batch_limit"] >= 1:
            try:
                torch.cuda.empty_cache()
                X_cand, Y_cand_pred = optimize_acqf(
                    acq_function=acq_func,
                    bounds=standard_bounds,
                    q=self.batch_size,
                    num_restarts=NUM_RESTARTS,
                    raw_samples=RAW_SAMPLES,  # used for intialization heuristic
                    options=options,
                    sequential=True,
                )
                torch.cuda.empty_cache()
                break
            except RuntimeError as e:
                if options["batch_limit"] > 1:
                    print(
                        "Got a RuntimeError in `optimize_acqf`. "
                        "Trying with reduced `batch_limit`."
                    )
                    options["batch_limit"] //= 2
                    continue
                else:
                    raise e 
        
        selection = {'x': np.array(X_cand), 'y': np.array(Y_cand_pred)}
        
        # self.solution = {'x': np.array(X), 'y': np.array(Y)}
        return selection


class qEHVISolver(NSGA2Solver):
    '''
    Solver based on PSL
    '''
    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)
        

    def solve(self, problem, X, Y, rho):
        standard_bounds = torch.zeros(2, problem.n_var, **tkwargs)
        standard_bounds[1] = 1
        surrogate_model = problem.surrogate_model
        
        ref_point = self.ref_point
        print("ref_point", ref_point)
        
        # use nsga2 to find pareto front for later plots, has limitations
        self.solution = super().solve(problem, X, Y)
        
        sampler = SobolQMCNormalSampler(sample_shape=torch.Size([MC_SAMPLES]))
        # solve surrogate problem
        # define acquisition functions
               
        with torch.no_grad():
            pred = problem.evaluate(X)
        pred = torch.tensor(pred).to(**tkwargs)
        
        partitioning = FastNondominatedPartitioning(
            ref_point=torch.tensor(ref_point).to(**tkwargs),
            Y=pred,
        )
        acq_func = qLogExpectedHypervolumeImprovement(
            ref_point=torch.tensor(ref_point).to(**tkwargs),
            model=surrogate_model.bo_model,
            partitioning=partitioning,
            sampler=sampler,
            
        )
        options = {"batch_limit": self.batch_size, "maxiter": 2000}
        
        while options["batch_limit"] >= 1:
            try:
                torch.cuda.empty_cache()
                X_cand, Y_cand_pred = optimize_acqf(
                    acq_function=acq_func,
                    bounds=standard_bounds,
                    q=self.batch_size,
                    num_restarts=NUM_RESTARTS,
                    raw_samples=RAW_SAMPLES,  # used for intialization heuristic
                    options=options,
                    sequential=True,
                )
                torch.cuda.empty_cache()
                break
            except RuntimeError as e:
                if options["batch_limit"] > 1:
                    print(
                        "Got a RuntimeError in `optimize_acqf`. "
                        "Trying with reduced `batch_limit`."
                    )
                    options["batch_limit"] //= 2
                    continue
                else:
                    raise e 
        
        selection = {'x': np.array(X_cand), 'y': np.array(Y_cand_pred)}
        
        return selection