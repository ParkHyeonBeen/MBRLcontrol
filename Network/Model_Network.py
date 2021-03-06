import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchbnn as bnn
from torch.optim import lr_scheduler
import numpy as np

from Common.Utils import weight_init

class DynamicsNetwork(nn.Module):
    def __init__(self, state_dim, action_dim, frameskip, algorithm, args, buffer=None,
                 net_type=None, hidden_dim=(256, 256)):
        super(DynamicsNetwork, self).__init__()

        if net_type is None:
            self.net_type = args.net_type
        else:
            self.net_type = net_type

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.frameskip = frameskip

        self.args = args
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.buffer4policy = algorithm.buffer
        self.buffer4model = buffer
        self.batch_size = args.batch_size

        if self.net_type == "DNN":

            self.dnmsNN_state = nn.ModuleList([nn.Linear(self.state_dim, int(hidden_dim[0]/2)), nn.ReLU()]).cuda()
            self.dnmsNN_action = nn.ModuleList([nn.Linear(self.action_dim, int(hidden_dim[0]/2)), nn.ReLU()]).cuda()
            self.dnmsNN = nn.ModuleList([nn.Linear(hidden_dim[0], hidden_dim[1]), nn.ReLU()])
            self.dnmsNN = self.dnmsNN.append(nn.Linear(hidden_dim[-1], state_dim)).cuda()

            self.model_lr = args.model_lr_dnn

        if self.net_type == "BNN":
            self.dnmsNN_state = nn.ModuleList([
                bnn.BayesLinear(prior_mu=0, prior_sigma=0.1,
                                in_features=self.state_dim, out_features=int(hidden_dim[0]/2)), nn.ReLU()]).cuda()
            self.dnmsNN_action = nn.ModuleList([
                bnn.BayesLinear(prior_mu=0, prior_sigma=0.1,
                                in_features=self.action_dim, out_features=int(hidden_dim[0]/2)), nn.ReLU()]).cuda()
            self.dnmsNN = nn.ModuleList([
                bnn.BayesLinear(prior_mu=0, prior_sigma=0.1, in_features=hidden_dim[0], out_features=hidden_dim[1]),
                nn.ReLU()])
            self.dnmsNN = self.dnmsNN.append(
                bnn.BayesLinear(prior_mu=0, prior_sigma=0.1, in_features=hidden_dim[1], out_features=self.state_dim)
            ).cuda()

            self.model_lr = args.model_lr_bnn

        self.dnms_optimizer = optim.Adam(self.dnmsNN.parameters(), lr=args.model_lr, weight_decay=0.01)
        # self.scheduler = lr_scheduler.ExponentialLR(self.dnms_optimizer, gamma=0.99)

        self.mse_loss = nn.MSELoss()
        self.kl_loss = bnn.BKLLoss(reduction='mean', last_layer_only=False)
        self.kl_weight = args.model_kl_weight

        self.apply(weight_init)

    def forward(self, state, action, train=False):

        if type(state) is not torch.Tensor:
            state = torch.tensor(state, dtype=torch.float).cuda()
        if type(action) is not torch.Tensor:
            action = torch.tensor(action, dtype=torch.float).cuda()

        state = F.softsign(state)
        for i in range(len(self.dnmsNN_state)):
            state = self.dnmsNN_state[i](state)
            action = self.dnmsNN_action[i](action)

        if train is True:
            z = torch.cat([state, action], dim=1)
        else:
            z = torch.cat([state, action])

        for i in range(len(self.dnmsNN)):
            z = self.dnmsNN[i](z)

        z = F.softsign(z)
        return z

    def train_all(self, training_num):
        cost = 0.0
        mse = 0.0
        kl = 0.0

        for i in range(training_num):

            if self.buffer4model is not None:
                s_pn, a_pn, _, ns_pn, _ = self.buffer4policy.sample(int(self.batch_size / 2))
                s_mn, a_mn, _, ns_mn, _ = self.buffer4model.sample(int(self.batch_size / 2))

                s = torch.cat([s_pn, s_mn])
                a = torch.cat([a_pn, a_mn])
                ns = torch.cat([ns_pn, ns_mn])

            else:
                s, a, _, ns, _ = self.buffer4policy.sample(self.batch_size)

            s_d = (ns - s) / self.frameskip
            s_d = F.softsign(s_d)

            z = self.forward(s, a, train=True)
            mse = self.mse_loss(z, s_d)
            kl = self.kl_loss(self.dnmsNN)
            cost = torch.mean(torch.abs(z - s_d)) + mse + self.kl_weight * kl

            self.dnms_optimizer.zero_grad()
            cost.backward()
            self.dnms_optimizer.step()
        # self.scheduler.step()

        cost = cost.cpu().detach().numpy()
        mse = mse.cpu().detach().numpy()
        kl = kl.cpu().detach().numpy()
        return cost, mse, kl

    def eval_model(self, state, action, next_state):

        state = torch.tensor(state, dtype=torch.float).cuda()
        action = torch.tensor(action, dtype=torch.float).cuda()
        next_state = torch.tensor(next_state, dtype=torch.float).cuda()
        state_d = (next_state - state)/self.frameskip

        # if self.net_type == "BNN":
        #     unfreeze(self.dnmsNN)

        z = self.forward(state, action)
        state_d = F.softsign(state_d)

        error = torch.max(torch.abs(z - state_d))
        error = error.cpu().detach().numpy()

        return error

class InverseDynamicsNetwork(nn.Module):
    def __init__(self, state_dim, action_dim,
                 frameskip, algorithm,
                 args, buffer=None, net_type=None, hidden_dim=(512, 512),
                 max_sigma=1e-2, min_sigma=1e-6, announce=True):
        super(InverseDynamicsNetwork, self).__init__()

        if net_type is None:
            self.net_type = args.net_type
        else:
            self.net_type = net_type

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.frameskip = frameskip

        self.args = args
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.buffer4policy = algorithm.buffer
        self.buffer4model = buffer
        self.batch_size = args.batch_size

        self.train_cnt = 0

        if self.net_type == "DNN":

            # self.inv_dnmsNN_state_d = nn.ModuleList([nn.Linear(self.state_dim, int(hidden_dim[0]/2)), nn.ReLU()]).cuda()
            # self.inv_dnmsNN_state = nn.ModuleList([nn.Linear(self.state_dim, int(hidden_dim[0]/2)), nn.ReLU()]).cuda()
            # self.inv_dnmsNN = nn.ModuleList([nn.Linear(hidden_dim[0], hidden_dim[1]), nn.ReLU()])
            # self.inv_dnmsNN = self.inv_dnmsNN.append(nn.Linear(hidden_dim[1], action_dim)).cuda()

            self.inv_dnmsNN= nn.ModuleList(
                [nn.Linear(self.state_dim*2, hidden_dim[0]), nn.ReLU()])
            self.inv_dnmsNN = self.inv_dnmsNN.append(nn.Linear(hidden_dim[0], hidden_dim[1]))
            self.inv_dnmsNN = self.inv_dnmsNN.append(nn.ReLU())
            self.inv_dnmsNN = self.inv_dnmsNN.append(nn.Linear(hidden_dim[1], action_dim)).cuda()

            self.inv_model_lr = args.inv_model_lr_dnn

        if self.net_type == "BNN":

            self.is_freeze = False

            self.inv_dnmsNN = nn.ModuleList([
                bnn.BayesLinear(prior_mu=0, prior_sigma=0.1,
                                in_features=self.state_dim*2, out_features=hidden_dim[0]), nn.ReLU()])
            self.inv_dnmsNN = self.inv_dnmsNN.append(
                bnn.BayesLinear(prior_mu=0, prior_sigma=0.1, in_features=hidden_dim[0], out_features=hidden_dim[1]))
            self.inv_dnmsNN = self.inv_dnmsNN.append(nn.ReLU())
            self.inv_dnmsNN = self.inv_dnmsNN.append(
                bnn.BayesLinear(prior_mu=0, prior_sigma=0.1, in_features=hidden_dim[1], out_features=self.action_dim)
            ).cuda()

            self.inv_model_lr = args.inv_model_lr_bnn

        if self.net_type == "prob":

            self.mu = torch.zeros(action_dim)
            self.sigma = torch.zeros(action_dim)

            self.fc = nn.Linear(self.state_dim*2, hidden_dim[0]).cuda()
            self.fc2 = nn.Linear(hidden_dim[0], hidden_dim[0]).cuda()
            self.ln = nn.LayerNorm(hidden_dim[0]).cuda()
            self.do1 = nn.Dropout(0.25)
            self.do2 = nn.Dropout(0.5)
            self.fc_mu = nn.Linear(hidden_dim[0], action_dim).cuda()
            self.fc_sigma = nn.Linear(hidden_dim[0], action_dim).cuda()

            self.max_sigma = max_sigma
            self.min_sigma = min_sigma
            assert (self.max_sigma >= self.min_sigma)
            if announce:
                print("Probabilistic transition model chosen.")

            self.inv_model_lr = args.inv_model_lr_dnn

        self.inv_dnms_optimizer = optim.Adam(self.parameters(), lr=self.inv_model_lr)
        # self.scheduler = lr_scheduler.ExponentialLR(self.inv_dnms_optimizer, gamma=0.99)

        self.mse_loss = nn.MSELoss()
        self.l1_loss = nn.L1Loss()
        self.kl_loss = bnn.BKLLoss(reduction='mean', last_layer_only=False)
        self.kl_weight = args.inv_model_kl_weight

        self.apply(weight_init)

    def forward(self, state, next_state, train=False):

        # BNN freezing part
        if self.net_type == 'BNN':
            if train is False and self.is_freeze is False:
                bnn.utils.eps_zero(self.inv_dnmsNN)
                self.is_freeze = True

            if train is True and self.is_freeze is True:
                bnn.utils.unfreeze(self.inv_dnmsNN)
                self.is_freeze = False

        # Tensorlizing
        if type(state) is not torch.Tensor:
            state = torch.tensor(state, dtype=torch.float).cuda()
        if type(next_state) is not torch.Tensor:
            next_state = torch.tensor(next_state, dtype=torch.float).cuda()

        state = F.softsign(state)
        next_state = F.softsign(next_state)

        state_d = (next_state - state)/(self.frameskip)

        # for i in range(len(self.inv_dnmsNN_state)):
        #     state_d = self.inv_dnmsNN_state_d[i](state_d)
        #     next_state = self.inv_dnmsNN_state[i](next_state)

        if train is True:
            z = torch.cat([state_d, next_state], dim=1)
        else:
            z = torch.cat([state_d, next_state])

        if self.net_type == "prob":

            z = self.fc(z)
            z = self.fc2(z)
            z = torch.relu(z)
            self.mu = self.fc_mu(z)
            if train is True:
                sigma = torch.sigmoid(self.fc_sigma(z))  # range (0, 1.)
                # scaled range (min_sigma, max_sigma)
                self.sigma = self.min_sigma + (self.max_sigma - self.min_sigma) * sigma
                z = self.sample_prediction(self.mu, self.sigma)
            else:
                z = self.mu
        else:
            for i in range(len(self.inv_dnmsNN)):
                z = self.inv_dnmsNN[i](z)

        z = torch.tanh(z)
        return z

    def sample_prediction(self, mu, sigma):
        eps = torch.randn_like(sigma)
        return mu + sigma * eps

    def train_all(self, training_num):

        self.train_cnt += 1

        cost = 0.0
        mse = 0.0
        kl = 0.0

        for i in range(training_num):

            if self.buffer4model is not None:
                s_pn, a_pn, _, ns_pn, _ = self.buffer4policy.sample(int(self.batch_size/2))
                s_mn, a_mn, _, ns_mn, _ = self.buffer4model.sample(int(self.batch_size/2))

                s = torch.cat([s_pn, s_mn])
                a = torch.cat([a_pn, a_mn])
                ns = torch.cat([ns_pn, ns_mn])

            else:
                s, a, _, ns, _ = self.buffer4policy.sample(self.batch_size)

            # s_d = (ns - s)/self.frameskip

            z = self.forward(s, ns, train=True)

            if self.net_type == "prob":

                mse = self.mse_loss(z, a)

                error_mean = torch.reshape(torch.tanh(self.mu) - a, (-1, 1, self.action_dim))
                error_mean_trans = torch.transpose(error_mean, 1, 2)

                cost = torch.mean(
                    torch.reshape(torch.matmul(torch.matmul(error_mean, torch.diag_embed(1/self.sigma)), error_mean_trans), (-1,))\
                    + torch.reshape(torch.log(torch.prod(self.sigma, dim=1)+1), (-1,))
                ) + self.l1_loss(z, a)

                kl = torch.Tensor(0)

            else:
                mse = self.mse_loss(z, a)
                kl = self.kl_loss(self.inv_dnmsNN)
                if self.train_cnt < 10000:
                    cost = mse + self.l1_loss(z, a)
                else:
                    cost = mse + self.kl_weight * kl + self.l1_loss(z, a)

            self.inv_dnms_optimizer.zero_grad()
            cost.backward()
            self.inv_dnms_optimizer.step()
        # self.scheduler.step()

        cost = cost.cpu().detach().numpy()
        mse = mse.cpu().detach().numpy()
        kl = kl.cpu().detach().numpy()
        return cost, mse, kl

    def eval_model(self, state, action, next_state):

        state = torch.tensor(state, dtype=torch.float).cuda()
        action = torch.tensor(action, dtype=torch.float).cuda()
        next_state = torch.tensor(next_state, dtype=torch.float).cuda()
        # state_d = (next_state - state)/self.frameskip

        z = self.forward(state, next_state)
        error = torch.max(torch.abs(z - action))
        error = error.cpu().detach().numpy()

        return error


if __name__ == '__main__':
    pass