#!/usr/bin/env python3
"""Generate portable and QNAP Compose files using a published GHCR image."""
import argparse
import importlib.util
from pathlib import Path
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer',root/'deploy/debian/install.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);p.add_argument('output',type=Path)
a=p.parse_args();m.registry_image(a.image);a.output.mkdir(parents=True,exist_ok=True)
s=(root/'deploy/compose.yaml').read_text().replace('# Prebuilt release. Load image.tar.gz before starting.','# Public GHCR image; Docker selects the matching CPU architecture.').replace('local/sonoff-protect-bridge:VERSION-ARCH',a.image).replace('    platform: linux/ARCH\n','').replace('    pull_policy: never\n','')
(a.output/'compose.yaml').write_text(s)
(a.output/'compose.qnap.yaml').write_text(s.replace('${ADMIN_BIND_IP:-127.0.0.1}:${ADMIN_PORT:-18080}','0.0.0.0:18080'))
