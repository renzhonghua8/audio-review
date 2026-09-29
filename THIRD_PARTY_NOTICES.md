# Third-party notices

## DNSMOS P.835 model

Source: Microsoft DNS Challenge, `DNSMOS/DNSMOS/sig_bak_ovr.onnx`.

Original URL: https://raw.githubusercontent.com/microsoft/DNS-Challenge/master/DNSMOS/DNSMOS/sig_bak_ovr.onnx

Repository and attribution: https://github.com/microsoft/DNS-Challenge

The model is distributed unchanged. Original SHA256:
`269fbebdb513aa23cddfbb593542ecc540284a91849ac50516870e1ac78f6edd`.

Microsoft and contributors license documentation and other repository content under Creative Commons Attribution 4.0 International (CC BY 4.0), and code under the MIT license, as stated in the repository's Legal Notices. The full license texts are in `licenses/DNS-CC-BY-4.0.txt` and `licenses/DNS-MIT.txt`.

License: https://creativecommons.org/licenses/by/4.0/
Legal notices: https://github.com/microsoft/DNS-Challenge#legal-notices

Reference:
Chandan K. A. Reddy, Vishak Gopal, Ross Cutler. “DNSMOS P.835: A Non-Intrusive Perceptual Objective Speech Quality Metric to Evaluate Noise Suppressors,” ICASSP 2022.

Inference preprocessing and polynomial calibration follow the official implementation:
https://github.com/microsoft/DNS-Challenge/blob/master/DNSMOS/dnsmos_local.py

This application performs local inference. It is not endorsed by Microsoft. The model outputs are estimates, not human judgments or authenticity determinations.

## Runtime dependencies

FastAPI, Uvicorn, python-multipart, NumPy, SoundFile, ONNX Runtime, imageio-ffmpeg, and their transitive packages are installed through PyPI. Their licenses and bundled FFmpeg notices remain in the installed distributions. The pinned versions used for validation are recorded in `requirements.lock.txt`.

The Docker image also installs Debian's FFmpeg, libsndfile, libgomp, and CA certificates packages, retaining their notices under `/usr/share/doc`. It uses the system FFmpeg executable via `IMAGEIO_FFMPEG_EXE`.
