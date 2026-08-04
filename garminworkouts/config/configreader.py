import yaml

from garminworkouts.config.includeloader import IncludeLoader


def read_config(filename):
    with open(filename) as f:
        data = yaml.load(f, IncludeLoader)
    return data
