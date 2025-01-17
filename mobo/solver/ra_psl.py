import numpy as np
from .solver import Solver

import numpy as np
import torch


from pymoo.util.nds.non_dominated_sorting import NonDominatedSorting

"""
A simple FC Pareto Set model.
"""

import torch
import torch.nn as nn

class ParetoSetModel(torch.nn.Module):
    def __init__(self, n_var, n_obj):
        super(ParetoSetModel, self).__init__()
        self.n_var = n_var
        self.n_obj = n_obj
       
        self.fc1 = nn.Linear(self.n_obj, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, self.n_var)
       
    def forward(self, pref):
        
        # tranform pref to spherical coordinates
        # sqared = pref**2
        # r = sqared.sum(axis=-1).sqrt()
        # phis=torch.zeros_like(pref)
        # phis[:,0] = r
        # for i in range(pref.shape[1]-1):
        #     phis[:,i+1] = torch.atan2(torch.pow(pref[:,i+1:],2).sum(axis=-1).sqrt(), pref[:,i])
        
        x = torch.relu(self.fc1(pref))
        x = torch.relu(self.fc2(x))
        x = self.fc3(x)
        
        x = torch.sigmoid(x) 
        
        return x.to(torch.float64)


# PSL 
# number of learning steps
n_steps = 1000
# number of sampled preferences per step
n_pref_update = 10 
# coefficient of LCB
coef_lcb = 0.1
# number of sampled candidates on the approxiamte Pareto front
n_candidate = 1000 
# number of optional local search
n_local = 0
# device
device = 'cpu'



class RAPSLSolver(Solver):
    '''
    Solver based on PSL
    '''
    def __init__(self, *args, **kwargs):
        self.z = torch.zeros(kwargs["n_obj"]).to(device)
        self.psmodel = None
        super().__init__(algo="",*args,**kwargs)
        
    def save_psmodel(self, path):
        torch.save(self.psmodel.state_dict(), f'{path}/psmodel.pt')

    def solve(self, problem, X, Y, rho=None):
        
        surrogate_model = problem.surrogate_model
        
        n_steps = 100 if hasattr(surrogate_model, "bo_model") else 500
        
        self.z =  torch.min(torch.cat((self.z.reshape(1,surrogate_model.n_obj),torch.from_numpy(Y).to(device) - 0.1)), axis = 0).values.data
        
        # nondominated X, Y 
        nds = NonDominatedSorting()
        idx_nds = nds.do(Y)
        
        X_nds = X[idx_nds[0]]
        Y_nds = Y[idx_nds[0]]
            
        # intitialize the model and optimizer 
        self.psmodel = ParetoSetModel(surrogate_model.n_var, surrogate_model.n_obj)
        self.psmodel.to(device)
            
        # optimizer
        optimizer = torch.optim.Adam(self.psmodel.parameters(), lr=1e-3)
          
        # t_step Pareto Set Learning with Gaussian Process
        self.psmodel.train()
        for t_step in range(n_steps):
            
            # sample n_pref_update preferences
            alpha = np.ones(surrogate_model.n_obj)
            pref = np.random.dirichlet(alpha,n_pref_update)
            pref_vec  = torch.tensor(pref).to(device).float() + 0.0001
            
            # get the current coressponding solutions
            x = self.psmodel(pref_vec)
            x_np = x.detach().cpu().numpy()
            
            
            out = surrogate_model.evaluate(x_np, std=True, noise=True, calc_gradient=True)
            
            mean = torch.from_numpy(out['F']).to(device)
            mean_grad = torch.from_numpy(out['dF']).to(device)
            std = torch.from_numpy(out['S']).to(device)
            std_grad = torch.from_numpy(out['dS']).to(device)
            rho_F = torch.from_numpy(out['rho_F']).to(device)
            drho_F = torch.from_numpy(out['drho_F']).to(device)
            rho_S = torch.from_numpy(out['rho_S']).to(device)
            drho_S = torch.from_numpy(out['drho_S']).to(device)

            gamma = 0.1
            coef_lcb_rho = 0.1
                
            # calculate the value/grad of tch decomposition with LCB
            value = mean - coef_lcb * std + gamma * (rho_F - coef_lcb_rho * rho_S)
            value_grad = mean_grad - coef_lcb * std_grad + gamma * (drho_F - coef_lcb_rho * drho_S)
            
            
            tch_idx = torch.argmax((1 / pref_vec) * (value - self.z), axis = 1)
            tch_idx_mat = [torch.arange(len(tch_idx)),tch_idx]
            tch_grad = (1 / pref_vec)[tch_idx_mat].view(n_pref_update,1) *  value_grad[tch_idx_mat] + 0.01 * torch.sum(value_grad, axis = 1) 

            tch_grad = tch_grad / torch.norm(tch_grad, dim = 1)[:, None]
            
            # gradient-based pareto set model update 
            optimizer.zero_grad()
            self.psmodel(pref_vec).backward(tch_grad)
            optimizer.step()  
            
        # solutions selection on the learned Pareto set
        self.psmodel.eval()
        
        # sample n_candidate preferences
        alpha = np.ones(surrogate_model.n_obj)
        pref = np.random.dirichlet(alpha,n_candidate)
        pref  = torch.tensor(pref).to(device).float() + 0.0001

        # generate correponding solutions, get the predicted mean/std
        X_candidate = self.psmodel(pref).to(torch.float64)
        X_candidate_np = X_candidate.detach().cpu().numpy()
        out = surrogate_model.evaluate(X_candidate_np, std=True)
        Y_candidate_mean, Y_candidata_std = out['F'], out['S']
        Y_candidate = Y_candidate_mean - coef_lcb * Y_candidata_std
        
        
        
        # construct solution
        self.solution = {'x': np.array(X_candidate_np), 'y': np.array(Y_candidate)}
        return self.solution