# SafeStep cinematic film

The final MP4 is `out/video.mp4`. The film is exactly 60 seconds, 1920 × 1080 at 60 FPS.

## Editable source

- `script.md`: narration and synchronization markers
- `scenes.js`: original deterministic Canvas animation, driven by Explainroo
- `video.json` and `safestep-theme.json`: format, typography, voice and audio settings
- `STORYBOARD.md`: creative treatment and shot plan
- `compose.py`: reproducible original stereo score
- `assets/`: music and bundled open fonts
- `build/timeline.json`: aligned word timestamps and shot timings
- `out/`: MP4, subtitles, cover, preview sheets and verification reports

## Rebuild

Requires Node.js 20.11+, Python 3 with NumPy (only to regenerate music), FFmpeg/FFprobe, and Chrome. Explainroo version 0.6.0 was used; see `production.json` for the installed version and exact dependency information.

```sh
npx --yes explainroo@0.6.0 doctor --fetch
npx --yes explainroo@0.6.0 voice motion/safestep-film
python3 motion/safestep-film/compose.py
npx --yes explainroo@0.6.0 check motion/safestep-film
npx --yes explainroo@0.6.0 sheet motion/safestep-film --every 2 --large
npx --yes explainroo@0.6.0 render motion/safestep-film --workers 2
npx --yes explainroo@0.6.0 verify motion/safestep-film
```

Run these from the SafeStep repository root. Set `EXPLAINROO_FFMPEG` and `EXPLAINROO_FFPROBE` if the binaries are outside PATH. The first voice run downloads the free Kokoro and Whisper models; narration and rendering run locally. No paid API or remote video generation is required.

The dashboard figures, pose sequence and notification are illustrative. Fonts retain their upstream licenses in `assets/fonts/licenses/`. The original music uses no external samples. Generated files under `build/` and `out/` are ignored by Git.
