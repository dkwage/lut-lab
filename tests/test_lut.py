"""Run: uv run python tests/test_lut.py"""
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

from lutlab import imaging
from lutlab import lut


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, output = root / "source.cube", root / "out.cube"
        table = lut.identity() ** 1.2
        lut.write_cube(source, table)
        command = [sys.executable, str(Path(lut.__file__)), "tweak", str(source), "-o", str(output)]
        subprocess.run(command, check=True)
        assert np.max(np.abs(lut.read_cube(output)[0] - table)) <= 1e-6
        print("PASS: no-option tweak reproduces input within 1e-6")
        subprocess.run(command + ["--strength", "1"], check=True)
        assert np.max(np.abs(lut.read_cube(output)[0] - table)) <= 1e-6
        print("PASS: strength 1 preserves full LUT behaviour within 1e-6")
        table = 1 - table
        lut.write_cube(source, table)
        for strength in (1.5, 2):
            subprocess.run(command + ["--strength", str(strength)], check=True)
            expected = np.clip(lut.identity() + strength * (table - lut.identity()), 0, 1)
            assert np.max(np.abs(lut.read_cube(output)[0] - expected)) <= 1e-6
        print("PASS: strengths 1.5 and 2 extrapolate a non-trivial cube and clip within 1e-6")
        expected = lut.identity() * .5 + np.clip((table - .5) * 2 + .5, 0, 1) * .5
        assert np.max(np.abs(lut.post_ops(table, contrast=100, strength=.5) - expected)) <= 1e-6
        print("PASS: post-ops still clip before strength")
        for value in ("2.01", "nan"):
            assert subprocess.run(command + ["--strength", value], capture_output=True).returncode != 0
        print("PASS: strengths 2.01 and nan are rejected")
        subprocess.run(command + ["--strength", "0", "--exposure", "1"], check=True)
        assert np.max(np.abs(lut.read_cube(output)[0] - lut.identity())) <= 1e-9
        print("PASS: strength 0 gives identity, including after post-ops")
        parsed = imaging.load_cube(output)
        assert tuple(parsed.size) == (33, 33, 33)
        assert Image.new("RGB", (1, 1), (128, 128, 128)).filter(parsed).getpixel((0, 0)) == (128, 128, 128)
        print("PASS: output parses and applies with lutlab.imaging.load_cube")
        lut.write_cube(source, lut.identity())
        subprocess.run(command + ["--exposure", "1"], check=True)
        midpoint = lut.read_cube(output)[0].reshape(33, 33, 33, 3)[16, 16, 16]
        assert np.all((midpoint > .68) & (midpoint < .69)), midpoint
        print(f"PASS: +1 EV mid grey = {midpoint.tolist()}")
        # Non-33 source, domain handling, all channels and red-fast ordering.
        small = (lut.identity(2) * 2 - .5, np.full(3, -.5), np.full(3, 1.5), 2)
        assert np.max(np.abs(lut.sample(small, lut.identity()) - lut.identity())) < 1e-12
        malformed = root / "bad.cube"
        malformed.write_text("LUT_3D_SIZE 33\n0 0 0\n")
        try:
            lut.read_cube(malformed)
        except ValueError:
            pass
        else:
            raise AssertionError("truncated cube accepted")
        print("PASS: resampling/domain/order and invalid-input checks")
    print("All LUT self-checks passed.")


if __name__ == "__main__":
    main()
