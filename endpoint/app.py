def build_command(req: PlayRequest, file_path: Path) -> str:
    ext = file_path.suffix.lower()

    kind = req.kind
    if kind == "auto":
        if ext == ".flac":
            kind = "flac"
        elif ext in [".wav", ".wave"]:
            kind = "wav"
        elif ext in [".pcm", ".raw"]:
            kind = "pcm"
        else:
            raise HTTPException(status_code=400, detail=f"Unknown file type: {ext}")

    f = shlex.quote(str(file_path))

    if kind == "flac":
        # Decode FLAC to WAV (with PCM S32_LE) so aplay can read the header (rate/channels).
        # This avoids packed 24-bit issues and prevents "slow playback" from missing sample rate info.
        return (
            f'/usr/bin/ffmpeg -loglevel error -i {f} -f wav -c:a pcm_s32le -ac 2 - | '
            f'/usr/bin/aplay -D {shlex.quote(ALSA_DEVICE)}'
        )

    if kind == "wav":
        return f'/usr/bin/aplay -D {shlex.quote(ALSA_DEVICE)} {f}'

    if kind == "pcm":
        if not req.pcm_format or not req.pcm_rate or not req.pcm_channels:
            raise HTTPException(
                status_code=400,
                detail="For kind=pcm, pcm_format, pcm_rate and pcm_channels are required",
            )
        return (
            f'/usr/bin/aplay -D {shlex.quote(ALSA_DEVICE)} '
            f'-f {shlex.quote(req.pcm_format)} '
            f'-r {int(req.pcm_rate)} '
            f'-c {int(req.pcm_channels)} {f}'
        )

    raise HTTPException(status_code=400, detail="Unsupported kind")
