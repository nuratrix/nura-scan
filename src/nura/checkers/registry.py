import importlib
import pkgutil

CHECKERS = {}


def checker(rule_id):
    def register(fn):
        CHECKERS[rule_id] = fn
        return fn
    return register


def load_checkers(domain):
    """Import every module under nura.checkers.<domain> so its @checker functions register.

    Rule domains use hyphens (cost-anomaly); package names use underscores.
    Relies on each package folder having an __init__.py (pkgutil skips folders without one).
    """
    pkg = importlib.import_module(f"nura.checkers.{domain.replace('-', '_')}")
    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{mod.name}")
