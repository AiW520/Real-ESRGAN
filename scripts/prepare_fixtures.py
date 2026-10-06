import json
import sys
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import DATA
import numpy as np
from PIL import Image, ImageDraw, ImageOps
from skimage import data

destination = DATA / 'fixtures'
destination.mkdir(parents=True, exist_ok=True)
photo = Image.fromarray(data.astronaut())
photo.resize((160,160),Image.Resampling.LANCZOS).save(destination/'portrait.png')
photo.save(destination/'photo.jpg',quality=95)
landscape = Image.fromarray(data.coffee())
for name, dimensions in [('720p', (1280,720)), ('1080p',(1920,1080)), ('4k',(3840,2160))]:
    ImageOps.fit(landscape, dimensions, Image.Resampling.LANCZOS).save(destination/f'{name}.jpg',quality=95)
with urllib.request.urlopen('https://api.github.com/repos/xinntao/Real-ESRGAN/contents/inputs',timeout=30) as response:
    entries=json.load(response)
print([(entry['name'],entry.get('download_url')) for entry in entries])
text=Image.new('RGB',(224,128),(245,248,251))
draw=ImageDraw.Draw(text)
draw.text((12,16),'AI Image Upscaler\nReal-ESRGAN 2x / 4x\n7680 x 4320',fill=(25,52,84),font_size=18)
text.save(destination/'text.png')
rgba=np.zeros((64,96,4),dtype='uint8')
rgba[:,:,:3]=(18,100,210)
rgba[:,:,3]=np.linspace(0,255,96,dtype='uint8')[None,:]
Image.fromarray(rgba).save(destination/'rgba.png')
print('Fixtures prepared:',destination)
