"""Deterministic contract fixture; not a color-science reference engine."""
import json
import sys
from pathlib import Path

from lutlab import lut

if sys.argv[1] == 'preset':
    print(json.dumps({'exposure': .2, 'colorGrading': {'shadows': {'hue': 200, 'saturation': 8}}}))
else:
    assert sys.argv[1] == 'adjustments'
    values = json.loads(Path(sys.argv[2]).read_text())
    table = lut.post_ops(lut.identity(), exposure=values.get('exposure', 0),
                         contrast=values.get('contrast', 0), saturation=values.get('saturation', 0))
    lut.write_cube(sys.argv[4], table)
