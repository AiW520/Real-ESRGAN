from types import SimpleNamespace
from pathlib import Path
import numpy as np
import torch
import pytest
from PIL import Image
from backend.app.model_manager import manager
from backend.app.tile_processor import infer_tile,process_tiled,process_with_fallback,close_map
import backend.app.tile_processor as tiles


def test_cuda_oom_schedule(monkeypatch,tmp_path):
    seen=[]
    def injected(rgb,model,target,size,workspace,**kw):
        seen.append(size)
        if size>128:
            raise torch.OutOfMemoryError('CUDA out of memory')
        return 'successful retry'
    monkeypatch.setattr(tiles,'process_tiled',injected)
    monkeypatch.setattr(torch.cuda,'empty_cache',lambda:None)
    value,attempts=process_with_fallback(None,'RealESRGAN_x4plus',SimpleNamespace(device='cuda'),(1,1),512,tmp_path,lambda *a:None,lambda:None)
    assert value=='successful retry'
    assert seen==[512,384,256,128]
    assert [x['tile_size'] for x in attempts]==seen


def test_accelerator_to_cpu_fallback(monkeypatch,tmp_path):
    seen=[]
    def injected(rgb,model,target,size,workspace,**kw):
        seen.append((model.device,size))
        if model.device=='cuda':
            raise torch.OutOfMemoryError('CUDA out of memory')
        return 'cpu succeeded'
    monkeypatch.setattr(tiles,'process_tiled',injected)
    monkeypatch.setattr(torch.cuda,'empty_cache',lambda:None)
    monkeypatch.setattr(manager,'release',lambda device:None)
    monkeypatch.setattr(manager,'load',lambda *a,**kw:SimpleNamespace(device=kw['device']))
    result,attempts=process_with_fallback(None,'RealESRGAN_x4plus',SimpleNamespace(device='cuda'),(1,1),128,tmp_path,lambda *a:None,lambda:None)
    assert result=='cpu succeeded'
    assert seen==[('cuda',128),('cuda',64),('cpu',128)]


@pytest.mark.real
def test_overlap_matches_full_neural_inference(tmp_path):
    from skimage import data
    from skimage.metrics import structural_similarity
    rgb=np.asarray(Image.fromarray(data.astronaut()).resize((96,96),Image.Resampling.LANCZOS))
    model=manager.load('RealESRGAN_x4plus_anime_6B',device='cpu')
    reference=infer_tile(model,rgb)
    output=process_tiled(rgb,model,(384,384),64,tmp_path)
    try:
        error=np.abs(reference.astype('float32')-output.astype('float32'))
        seam=error[:,188:196].mean()
        assert error.mean()<2.0,(error.mean(),seam)
        assert seam<3.0,seam
        assert structural_similarity(reference,np.asarray(output),channel_axis=2,data_range=255)>.97
        assert not np.all(output[0]==0) and not np.all(output[-1]==0)
    finally:
        close_map(output)
