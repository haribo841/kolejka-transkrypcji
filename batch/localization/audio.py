"""Native Instrumental stem using existing sample-clock recovery, in a separate workspace."""
import argparse
from pathlib import Path

from ..common import atomic_json, digest, read_json, signature
from ..media import EmptyAudio
from ..paths import child_path, input_file
from ..uvr_recovery import VocalRecovery
from . import directory


def background_key(source, config):
    return signature({"source": digest(source), "model": config["uvr_model"],
                      "model_sha256": digest(child_path(config["uvr_model_dir"], config["uvr_model"])),
                      "seconds": config["uvr_seconds"], "context": config["uvr_context"],
                      "stem": "Instrumental", "format": 1})


class BackgroundAPI:
    def __init__(self, request):
        from .. import uvr_child
        self.request = request
        self.separator = uvr_child.separator
        self.separate_vocal = uvr_child.separate_vocal

    @staticmethod
    def emit(message, percent=0):
        from ..uvr_child import emit
        emit(message.replace("wokalu", "podkładu").replace("wokal", "podkład"), percent)

    def request_paths(self, request):
        base = directory(request["root"], request["video_id"])
        work = child_path(base, "robocze-podklad")
        output = child_path(base, "audio/bez-glosow.flac")
        if Path(request["work"]) != work or Path(request["output"]) != output:
            raise ValueError("Niepoprawne miejsce zapisu podkładu.")
        return input_file(request["source"]), work, output


class BackgroundRecovery(VocalRecovery):
    def adopt_legacy(self):
        # Vocal-only checkpoints must never be adopted as instrumental audio.
        return


def run(request):
    request["config"] = {**request["config"], "uvr_stem": "Instrumental"}
    request["signature"] = background_key(request["source"], request["config"])
    work = Path(request["work"])
    work.mkdir(parents=True, exist_ok=True)
    recovery = BackgroundRecovery(request, BackgroundAPI(request))
    recovery.run()
    result = read_json(work / "uvr-result.json", {})
    if not result.get("complete"):
        raise RuntimeError("Podkład jest częściowy. Zachowano poprawne bloki; ponów przygotowanie.")
    if Path(result["output"]) != Path(request["output"]) or digest(request["output"]) != result["sha256"]:
        raise RuntimeError("Niepoprawny wynik podkładu UVR.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = read_json(args.request)
    try:
        run(request)
    except EmptyAudio:
        atomic_json(Path(request["work"]) / "uvr-result.json", {"empty_audio": True, "complete": True, "source_complete": True})


if __name__ == "__main__":
    main()
