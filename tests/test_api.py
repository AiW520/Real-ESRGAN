import io
import time
from PIL import Image
import pytest
from backend.app.jobs import Job


def image_bytes(mode='RGB', fmt='PNG'):
    value = Image.new(mode, (24,16), (210,50,20,100) if mode=='RGBA' else (210,50,20))
    stream = io.BytesIO()
    value.save(stream, format=fmt)
    return stream.getvalue()


def wait(client, job_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = client.get(f'/api/upscale/{job_id}/status').json()
        if status['status'] in ('completed','failed','cancelled'):
            return status
        time.sleep(.1)
    pytest.fail('Job timed out in the integration test.')


@pytest.mark.real
@pytest.mark.parametrize('scale,fmt,mode',[(1,'png','RGB'),(2,'png','RGBA'),(4,'jpg','RGB'),(2,'webp','RGBA')])
def test_real_upload_download(client, scale, fmt, mode):
    api, store = client
    response = api.post('/api/upscale', files={'file':('../../evil.png',image_bytes(mode),'image/png')},
                        data={'scale':scale,'output_format':fmt})
    assert response.status_code == 200, response.text
    status = wait(api,response.json()['job_id'])
    assert status['status']=='completed',status
    assert status['original_name']=='evil.png'
    assert status['tiles_done']==status['tiles_total']>0
    result = api.get(f'/api/upscale/{status["job_id"]}/download')
    assert result.status_code == 200 and len(result.content)>100
    with Image.open(io.BytesIO(result.content)) as image:
        image.load()
        assert image.size == (24*scale,16*scale)
        assert image.info.get('icc_profile')
        assert image.getpixel((image.width//2,image.height//2))[0] > image.getpixel((0,0))[2]
        if mode=='RGBA' and fmt!='jpg':
            assert image.mode=='RGBA'
            assert image.getchannel('A').getextrema()==(100,100)
    assert len(list(store.data.glob('uploads/*'))) == 0


@pytest.mark.parametrize('data,match',[
    ({'model':'unknown'},'model'),({'scale':3},'Scale'),({'tile_size':100},'Tile'),
    ({'quality':101},'Quality'),({'target_width':400,'target_height':100},'aspect'),
    ({'target_resolution':'8K'},'4x')])
def test_invalid_options(client,data,match):
    api,_=client
    response=api.post('/api/upscale',files={'file':('x.png',image_bytes(),'image/png')},data=data)
    assert response.status_code==400,response.text
    assert match.lower() in response.json()['detail'].lower()


def test_content_validation_and_origin(client):
    api,_=client
    bad=api.post('/api/upscale',files={'file':('fake.jpg',b'not an image','image/jpeg')})
    assert bad.status_code==400
    denied=api.post('/api/upscale',headers={'origin':'https://evil.example'},files={'file':('x.png',image_bytes(),'image/png')})
    assert denied.status_code==403


def test_cancel_real_job(client):
    api,store=client
    response=api.post('/api/upscale',files={'file':('x.png',image_bytes(),'image/png')})
    job_id=response.json()['job_id']
    api.post(f'/api/upscale/{job_id}/cancel')
    status=wait(api,job_id)
    assert status['status']=='cancelled'
    assert api.get(f'/api/upscale/{job_id}/download').status_code==404


def test_queue_limit(client):
    api,store=client
    for index in range(4):
        job=Job(f'{index:032x}')
        store.items[job.job_id]=job
    response=api.post('/api/upscale',files={'file':('x.png',image_bytes(),'image/png')})
    assert response.status_code==429
