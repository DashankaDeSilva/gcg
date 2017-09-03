import os

from sandbox.gkahn.rnn_critic.envs.rccar.square_env import SquareEnv

class SquareClutteredEnv(SquareEnv):
    def __init__(self, params={}):
        params.setdefault('model_path', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models/square_cluttered.egg'))

        SquareEnv.__init__(self, params=params)

if __name__ == '__main__':
    params = {'visualize': True, 'run_as_task': True}
    env = SquareClutteredEnv(params)
