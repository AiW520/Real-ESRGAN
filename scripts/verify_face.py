import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.config import DATA
from backend.app.face_enhancer import face_enhancer
from backend.app.image_processor import read_pixels,encode
from backend.app.model_manager import manager
rgb,_=read_pixels(DATA/'fixtures'/'photo.jpg')
print('Loading official GFPGAN and face detection/parsing weights',flush=True)
output,count=face_enhancer.enhance(rgb)
print('Restored faces:',count,flush=True)
assert count>0,'The portrait fixture should contain a detected face.'
destination=DATA/'validation'/'face-enhanced.png'
destination.parent.mkdir(parents=True,exist_ok=True)
encode(output,None,destination,'png',95)
assert output.shape==rgb.shape
print('Output:',destination,flush=True)
