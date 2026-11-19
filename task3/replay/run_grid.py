from pathlib import Path
from prepare_grid import prepare
from core.grid import evaluate

base=Path(__file__).resolve().parent/'calibration'
binary=base/'bars.bin'
prepare(binary)
evaluate(base,binary)
