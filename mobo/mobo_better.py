import numpy as np
from .surrogate_problem import SurrogateProblem
from .utils import Timer, find_pareto_front, calc_hypervolume, find_pareto_front_old, calc_hypervolume_old
from .factory import init_from_config
from .transformation import StandardTransform
'''
Main algorithm framework for Multi-Objective Bayesian Optimization
'''

class MOBO:
    '''
    Base class of algorithm framework, inherit this class with different configs to create new algorithm classes
    '''
    config = {}

    def __init__(self, problem, n_iter, ref_point, framework_args):
        '''
        Input:
            problem: the original / real optimization problem
            n_iter: number of iterations to optimize
            ref_point: reference point for hypervolume calculation
            framework_args: arguments to initialize each component of the framework
        '''
        self.real_problem = problem
        self.n_var, self.n_obj = problem.n_var, problem.n_obj
        self.n_iter = n_iter
        self.ref_point = ref_point

        bounds = np.array([problem.xl, problem.xu])
        self.transformation = StandardTransform(bounds) # data normalization for surrogate model fitting

        # framework components
        framework_args['surrogate']['n_var'] = self.n_var # for surrogate fitting
        framework_args['surrogate']['n_obj'] = self.n_obj # for surroagte fitting
        framework_args['solver']['n_obj'] = self.n_obj # for MOEA/D-EGO
        framework = init_from_config(self.config, framework_args)
        
        self.surrogate_model = framework['surrogate'] # surrogate model
        self.acquisition = framework['acquisition'] # acquisition function
        self.solver = framework['solver'] # multi-objective solver for finding the paretofront
        self.selection = framework['selection'] # selection method for choosing new (batch of) samples to evaluate on real problem
        
        # to keep track of data and pareto information (current status of algorithm)
        self.X = None
        self.Y = None
        self.sample_num = 0
        self.status = {
            'pset': None,
            'pfront': None,
            'hv': None,
            'ref_point': self.ref_point,
        }

        # other component-specific information that needs to be stored or exported
        self.info = None

    def _update_status(self, X, Y):
        '''
        Update the status of algorithm from data
        '''
        if self.sample_num == 0:
            self.X = X
            self.Y = Y
        else:
            self.X = np.vstack([self.X, X])
            self.Y = np.vstack([self.Y, Y])
        self.sample_num += len(X)

        self.status['pfront'], pfront_idx = find_pareto_front(self.Y, return_index=True)
        self.status['pset'] = self.X[pfront_idx]
        self.status['hv'] = calc_hypervolume(self.status['pfront'], self.ref_point)
        print(f'Current Hypervolume: {self.status["hv"]:.4f}')
        print(f'Cuurent old Hypervolume: {calc_hypervolume_old(self.status["pfront"], self.ref_point):.4f}')
        print(f'Current pareto front size: {len(self.status["pfront"])}\n')
        print(f'Current old pareto front size: {len(find_pareto_front_old(self.Y))}\n')
        

    def solve(self, X_init, Y_init):
        '''
        Solve the real multi-objective problem from initial data (X_init, Y_init)
        '''
        # determine reference point from data if not specified by arguments
        if self.ref_point is None:
            # self.ref_point = np.min(Y_init, axis=0)
            # print("ref_point", self.ref_point)
            # self.solver.ref_point = np.min(Y_init, axis=0) # ref point is different for botorch
            # print("ref_point solver", self.solver.ref_point)
            import torch
            from botorch.utils.multi_objective.hypervolume import infer_reference_point
            self.ref_point = infer_reference_point(torch.tensor(Y_init)).numpy()
            print("ref_point", self.ref_point)
        self.selection.set_ref_point(self.ref_point)
        self.solver.set_ref_point(self.ref_point)

        self._update_status(X_init, Y_init)

        global_timer = Timer()
        
        from botorch.utils.multi_objective.box_decompositions.dominated import (
            DominatedPartitioning,
        )
        import torch
        
        hvs = []
        
        for i in range(self.n_iter):
            print('========== Iteration %d ==========' % i)

            timer = Timer()

            # data normalization
            self.transformation.fit(self.X, self.Y)
            X, Y = self.transformation.do(self.X, self.Y)

            # build surrogate models
            self.surrogate_model.fit(X, Y)
            timer.log('Surrogate model fitted')

            # define acquisition functions
            self.acquisition.fit(X, Y)

            # solve surrogate problem
            surr_problem = SurrogateProblem(self.real_problem, self.surrogate_model, self.acquisition, self.transformation)
            solution = self.solver.solve(surr_problem, X, Y)
            timer.log('Surrogate problem solved')

            # batch point selection
            self.selection.fit(X, Y)
            X_next, self.info = self.selection.select(solution, self.surrogate_model, self.status, self.transformation)
            timer.log('Next sample batch selected')

            # update dataset
            Y_next = self.real_problem.evaluate(X_next)
            if self.real_problem.n_constr > 0: Y_next = Y_next[0]
            self._update_status(X_next, Y_next)
            timer.log('New samples evaluated')

            # statistics
            global_timer.log('Total runtime', reset=False)
            print('Total evaluations: %d, hypervolume: %.4f\n' % (self.sample_num, self.status['hv']))
            
            train_Y = torch.from_numpy(self.Y)
            
            for hvs_list_i, train_Y_i in zip((hvs,), (train_Y,)):
                # compute hypervolume
                bd = DominatedPartitioning(
                    ref_point=torch.tensor(self.solver.ref_point),
                    Y=train_Y_i
                )
                volume = bd.compute_hypervolume().item()
                hvs_list_i.append(volume)
                
            print(
                f"\nBatch {i:>2}: Hypervolume (qNEHVI) = "
                f"( {hvs[-1]:>4.2f}), "
            )
            # return new data iteration by iteration
            yield X_next, Y_next

    def __str__(self):
        return \
            '========== Framework Description ==========\n' + \
            f'# algorithm: {self.__class__.__name__}\n' + \
            f'# surrogate: {self.surrogate_model.__class__.__name__}\n' + \
            f'# acquisition: {self.acquisition.__class__.__name__}\n' + \
            f'# solver: {self.solver.__class__.__name__}\n' + \
            f'# selection: {self.selection.__class__.__name__}\n'