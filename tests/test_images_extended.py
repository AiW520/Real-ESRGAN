import numpy as np
import pytest
from PIL import Image
from backend.app.image_processor import target_size,inspect_image,read_pixels,encode,SRGB_PROFILE


@pytest.mark.parametrize('input_size,scale,resolution,expected',[
    ((1280,720),2,'scale',(2560,1440)),((1920,1080),2,'scale',(3840,2160)),
    ((3840,2160),2,'8K',(7680,4320)),((2160,3840),2,'8K',(4320,7680)),
    ((2000,2000),2,'8K',(5760,5760)),((100,60),4,'original',(100,60))])
def test_dimensions(input_size,scale,resolution,expected):
    assert target_size(*input_size,scale,resolution)==expected


def test_limits():
    with pytest.raises(ValueError,match='exceeds'):
        target_size(3840,2160,4)
    with pytest.raises(ValueError,match='smaller'):
        target_size(4000,3000,2,'2K')


def test_exif_icc_alpha(tmp_path):
    path=tmp_path/'oriented.jpg'
    exif=Image.Exif();exif[274]=6
    Image.new('RGB',(40,20),(180,20,60)).save(path,exif=exif,icc_profile=SRGB_PROFILE,quality=100)
    assert (inspect_image(path).width,inspect_image(path).height)==(20,40)
    rgb,alpha=read_pixels(path)
    assert rgb.shape==(40,20,3) and alpha is None
    assert np.abs(rgb[0,0].astype('int32')-np.array([180,20,60])).max()<4
    rgba=tmp_path/'rgba.png'
    image=Image.new('RGBA',(24,16),(20,70,210,95));image.save(rgba)
    rgb,alpha=read_pixels(rgba)
    dest=tmp_path/'out.webp'
    encode(np.tile(rgb,(2,2,1)),alpha,dest,'webp',95)
    with Image.open(dest) as output:
        output.load()
        assert output.size==(48,32) and output.mode=='RGBA'
        assert output.getchannel('A').getextrema()==(95,95)


def test_gray_tiff_and_16_bit_rejection(tmp_path):
    path=tmp_path/'gray.tiff'
    Image.new('L',(24,16),120).save(path)
    assert inspect_image(path).format=='TIFF'
    rgb,_=read_pixels(path)
    assert np.all(rgb==120)
    path16=tmp_path/'16.png'
    Image.fromarray(np.zeros((16,24),dtype='uint16')).save(path16)
    with pytest.raises(ValueError,match='16-bit'):
        inspect_image(path16)


def test_nonstandard_png_gamma(tmp_path):
    import struct
    from PIL.PngImagePlugin import PngInfo
    metadata=PngInfo();metadata.add(b'gAMA',struct.pack('>I',50000))
    path=tmp_path/'gamma.png'
    Image.new('RGB',(24,16),(128,128,128)).save(path,pnginfo=metadata)
    rgb,_=read_pixels(path)
    assert tuple(rgb[0,0])==(137,137,137)
