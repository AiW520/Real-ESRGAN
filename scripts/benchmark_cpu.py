import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.model_manager import manager
import torch

model = manager.load('RealESRGAN_x4plus').network
for layout in (torch.contiguous_format, torch.channels_last):
    model.to(memory_format=layout)
    for threads in (1,2,4,8):
        torch.set_num_threads(threads)
        value=torch.rand(1,3,128,128).contiguous(memory_format=layout)
        started=time.perf_counter()
        with torch.inference_mode():
            result=model(value)
        print(str(layout),threads,round(time.perf_counter()-started,3),tuple(result.shape),flush=True)
