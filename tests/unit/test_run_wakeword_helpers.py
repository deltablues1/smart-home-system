from scripts.run_wakeword import pcm16_rms, pcm16_to_wav_bytes


def test_pcm16_rms_zero_for_silence():
    assert pcm16_rms(b"\x00\x00" * 64) == 0.0


def test_pcm16_to_wav_bytes_has_riff_header():
    wav_bytes = pcm16_to_wav_bytes([b"\x01\x00\x02\x00" * 32], sample_rate=16000)

    assert wav_bytes[:4] == b"RIFF"
    assert b"WAVE" in wav_bytes[:16]
