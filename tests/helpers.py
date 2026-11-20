import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cauce.audit import AuditLog  # noqa: E402
from cauce.config import load  # noqa: E402
from cauce.engine import Engine  # noqa: E402
from cauce.simulator import SimulatedNetwork, three_links  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def sim_engine(service=None, links=None, **cfg_over):
    d = tempfile.mkdtemp(prefix="cauce-test-")
    over = {"data_dir": d, "health": {"targets": ["sim.diag:443"]}, "lab": {"allowed_hosts": ["lab.sim", "127.0.0.1"]}}
    for k, v in cfg_over.items():
        over.setdefault(k, {}).update(v) if isinstance(v, dict) else over.__setitem__(k, v)
    cfg = load(overrides=over)
    net = SimulatedNetwork(links or three_links(), service) if service else SimulatedNetwork(links or three_links())
    eng = Engine(cfg, net, net, audit=AuditLog(os.path.join(d, "audit.jsonl")), mode="simulated")
    eng.refresh()
    eng.health.check_all(force=True)
    return eng, net
