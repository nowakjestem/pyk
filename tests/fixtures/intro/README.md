# Logo → title → live footage regression samples

These three 128×72 RGB24 frame sequences reproduce the premature title-card cuts
reported on 2026-10-26 for job `979702ada3d7469d8a70087ab857a3ff`.
They come directly from the original video, before output cropping, captions or
another video encode: <https://www.youtube.com/watch?v=qgmpGYgrFQ0>.
They contain no audio. `.rgb.zlib` is a zlib-compressed sequence of raw frames;
the corresponding FFmpeg framecrc `.txt` supplies the original per-frame timestamps.
No network access or FFmpeg is needed to replay these regression tests.

| Sample | Chapter start in original | Captured duration | First live frame index (zero based) | First live frame PTS |
| --- | --- | --- | --- | --- |
| 004 / Zarobki lekarzy | 434 s | 2.2 s | 55 | 1.8354 s |
| 005 / Trump i Putin | 508 s | 2.9 s | 76 | 2.5433666666666666 s |
| 006 / 8 lat to za mało | 604 s | 2.9 s | 76 | 2.539266666666667 s |

The last title frame and first live frame of each sample were visually checked.
The previous detector cut these samples at 0.2338, 0.9417666666666666 and
0.9376666666666666 s, respectively, mistaking the switch from the small white logo
to denser, antialiased title text for the end of the intro. Roughly 1.6 s of each
title remained. Differences of about 8 ms from the original job checkpoints come
from the downloaded source container's video offset.
