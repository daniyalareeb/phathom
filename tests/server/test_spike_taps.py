"""serve_spike track routing: one wav per speaker:N tap label."""

from phathom.serve_spike import _wav_name


def test_classic_tracks_keep_fixed_names():
    assert _wav_name("caller") == "caller.wav"
    assert _wav_name("caller:2") == "caller.wav"
    assert _wav_name("owner") == "owner.wav"
    assert _wav_name("speaker") == "speaker.wav"


def test_speaker_tap_labels_get_own_wavs():
    assert _wav_name("speaker:1(AudioWorkletNode)") == "speaker_1_audioworkletnode.wav"
    assert _wav_name("speaker:2(MediaStreamAudioSourceNode)") == "speaker_2_mediastreamaudiosourcenode.wav"


def test_sink_writes_per_label(tmp_path):
    from phathom.serve_spike import CallWavs
    wavs = CallWavs(tmp_path / "call")
    wavs.write("speaker:1(AudioWorkletNode)", b"\x01\x02" * 100)
    wavs.write("owner", b"\x03\x04" * 100)
    wavs.close()
    assert (tmp_path / "call" / "speaker_1_audioworkletnode.wav").exists()
    assert (tmp_path / "call" / "owner.wav").exists()
    assert wavs.track_seconds("speaker:1(AudioWorkletNode)") > 0
