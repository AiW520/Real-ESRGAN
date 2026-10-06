import sys
import threading
import numpy as np
import torch
import torchvision.transforms.functional as functional

from .model_manager import download_weight
from .config import MODELS

# BasicSR 1.4.2 imports a torchvision module removed in torchvision 0.17.
sys.modules.setdefault('torchvision.transforms.functional_tensor', functional)


class FaceEnhancer:
    def __init__(self):
        self.cache = None
        self.lock = threading.Lock()

    def load(self, cancel=lambda: None):
        if self.cache:
            return self.cache
        from facexlib.utils.face_restoration_helper import FaceRestoreHelper
        from gfpgan.archs.gfpganv1_clean_arch import GFPGANv1Clean
        paths = {}
        sources = {
            'GFPGANv1.4': 'https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth',
            'detection_Resnet50_Final': 'https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth',
            'parsing_parsenet': 'https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth',
        }
        for name, url in sources.items():
            paths[name] = download_weight(name, url, cancel)
        cancel()
        # The optional face path uses CPU FP32 to preserve accelerator memory for SR.
        network = GFPGANv1Clean(out_size=512, num_style_feat=512, channel_multiplier=2,
                              decoder_load_path=None, fix_decoder=False, num_mlp=8,
                              input_is_latent=True, different_w=True, narrow=1, sft_half=True)
        state = torch.load(paths['GFPGANv1.4'], map_location='cpu', weights_only=True)
        network.load_state_dict(state.get('params_ema', state.get('params', state)), strict=True)
        network.eval().requires_grad_(False)
        helper = FaceRestoreHelper(1, face_size=512, det_model='retinaface_resnet50', use_parse=True,
                                   device=torch.device('cpu'), model_rootpath=str(MODELS))
        self.cache = network, helper
        return self.cache

    def enhance(self, rgb, cancel=lambda: None, progress=lambda *_: None):
        with self.lock:
            network, helper = self.load(cancel)
            helper.clean_all()
            try:
                helper.read_image(np.ascontiguousarray(rgb[:, :, ::-1]))
                helper.get_face_landmarks_5(resize=640, eye_dist_threshold=5)
                cancel()
                if not helper.all_landmarks_5:
                    return rgb, 0
                helper.align_warp_face()
                for index, face in enumerate(helper.cropped_faces):
                    cancel()
                    value = np.ascontiguousarray(face[:, :, ::-1].transpose(2, 0, 1), dtype='float32') / 127.5 - 1
                    with torch.inference_mode():
                        restored = network(torch.from_numpy(value).unsqueeze(0), return_rgb=False, weight=0.5)[0]
                        result = restored.squeeze(0).clamp_(-1, 1).add_(1).mul_(127.5).round_().permute(1, 2, 0).numpy().astype('uint8')
                    helper.add_restored_face(np.ascontiguousarray(result[:, :, ::-1]))
                    progress(index + 1, len(helper.cropped_faces))
                cancel()
                helper.get_inverse_affine(None)
                restored = helper.paste_faces_to_input_image(upsample_img=helper.input_img)
                result = np.ascontiguousarray(restored[:, :, ::-1])
                if result.shape != rgb.shape:
                    raise RuntimeError('Face restoration changed output dimensions.')
                return result, len(helper.cropped_faces)
            finally:
                helper.clean_all()
                helper.input_img = None


face_enhancer = FaceEnhancer()
