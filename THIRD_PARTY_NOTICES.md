# Third-party sources

This repository is an application using Real-ESRGAN models, not the official Real-ESRGAN repository.

## BasicSR RRDB architecture

`backend/app/rrdb.py` is a compact adaptation of the RRDBNet architecture and state-dict layout documented in BasicSR. It includes the RGB x4 networks needed by this service and omits the upstream architecture registry and other variants.

Source: https://github.com/XPixelGroup/BasicSR/blob/master/basicsr/archs/rrdbnet_arch.py

Copyright 2018-2022 BasicSR Authors.
Licensed under the Apache License, Version 2.0. A copy is included in `licenses/BasicSR-Apache-2.0.txt`.

## Real-ESRGAN official models

The service downloads model weights from official releases of https://github.com/xinntao/Real-ESRGAN.

Real-ESRGAN is copyright (c) 2021, Xintao Wang, under the BSD 3-Clause license. A copy of its upstream license is included in `licenses/Real-ESRGAN-BSD-3-Clause.txt`. Weights are not included in this Git repository; their usage remains subject to the upstream terms.

## Runtime dependencies

GFPGAN, facexlib, PyTorch, torchvision, FastAPI, React, Lucide and other installed dependencies retain their own licenses. They are installed separately through the Python requirements and npm lockfile. This notice does not relicense third-party dependencies or grant access to the Tuzi image generation API.
