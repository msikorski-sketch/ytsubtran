#!/usr/bin/env python
"""
DaVinci Resolve — add timeline markers at detected inserts.

WHAT IT DOES
  Reads a cut list produced by ytsubtran (a "<video>_inserts.txt" file) and drops
  a coloured marker on the CURRENT timeline at every insert, so you can jump
  between them instead of scrubbing the whole video.

HOW TO INSTALL (free/Standard or Studio)
  Copy this file into Resolve's script folder, e.g. on Windows:
    %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\
  Then in Resolve:  Workspace → Scripts → resolve_markers
  (If your Resolve build blocks scripting, use the SRT-subtitle export instead.)

HOW IT PICKS THE RIGHT VIDEO (robust to "wrong clip open")
  1. It looks at the source video of the CURRENT timeline and tries to find the
     matching "<video>_inserts.txt" right next to that file.
  2. If the open timeline doesn't match any list, it scans that folder and shows a
     PICKER of the lists it found (and clearly warns about the mismatch). Markers
     are always added to the CURRENT timeline — so open the right video first.

NOTE: marker frames are measured from the start of the timeline, so import the
exact analysed video as a timeline that starts at its first frame.
"""
import glob
import os

# ---------------------------------------------------------------------------
# CONFIG — tweak if you like
# ---------------------------------------------------------------------------
MARKER_COLOR = 'Yellow'   # Blue/Cyan/Green/Yellow/Red/Pink/Purple/Fuchsia/Rose/...
MARKER_SPAN = True        # True: marker spans start→end; False: single point at start
CUTLIST = ''              # optional: hard-code a path to a *_inserts.txt to use
SCAN_FOLDER = ''          # optional: folder to scan for lists (else: timeline's folder)


# ---------------------------------------------------------------------------
# Pure helpers (no Resolve needed — unit-testable)
# ---------------------------------------------------------------------------

def parse_time(value):
    """'M:SS' / 'H:MM:SS' / seconds → float seconds."""
    s = str(value).strip()
    if ':' in s:
        total = 0.0
        for part in s.split(':'):
            total = total * 60 + float(part)
        return total
    return float(s)


def load_cut_list(path):
    """Reads a ytsubtran cut list. Ignores blank/'#' lines. Returns [(start, end, reason)]."""
    out = []
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            reason = ''
            if '#' in line:
                line, reason = line.split('#', 1)
                reason = reason.strip()
            parts = line.replace(',', ' ').split()
            if len(parts) < 2:
                continue
            try:
                s, e = parse_time(parts[0]), parse_time(parts[1])
            except ValueError:
                continue
            if e > s:
                out.append((s, e, reason))
    return sorted(out)


def seconds_to_frame(seconds, fps):
    """Frame index from the start of the timeline."""
    return int(round(float(seconds) * float(fps)))


def list_for_video(video_path, lists):
    """Returns the cut list whose name matches the video's base name, else None."""
    if not video_path:
        return None
    base = os.path.splitext(os.path.basename(video_path))[0]
    exact = f'{base}_inserts.txt'
    for p in lists:
        if os.path.basename(p) == exact:
            return p
    for p in lists:
        if os.path.basename(p).startswith(base):
            return p
    return None


# ---------------------------------------------------------------------------
# Resolve glue (guarded so this module imports fine without Resolve)
# ---------------------------------------------------------------------------

def get_resolve():
    """Returns the Resolve app object, or None if not reachable."""
    r = globals().get('resolve')          # injected when run inside Resolve
    if r is not None:
        return r
    try:
        import DaVinciResolveScript as dvr   # external scripting
        return dvr.scriptapp('Resolve')
    except Exception:
        return None


def timeline_video_files(timeline):
    """Source file paths of the video clips on the timeline (best effort)."""
    files = []
    try:
        track_count = int(timeline.GetTrackCount('video'))
    except Exception:
        track_count = 1
    for i in range(1, track_count + 1):
        try:
            items = timeline.GetItemListInTrack('video', i) or []
        except Exception:
            items = []
        for item in items:
            try:
                mpi = item.GetMediaPoolItem()
                if mpi:
                    path = mpi.GetClipProperty('File Path')
                    if path:
                        files.append(path)
            except Exception:
                pass
    return files


def get_fps(project, timeline):
    """Timeline frame rate as a float (falls back to 24)."""
    for getter in (lambda: timeline.GetSetting('timelineFrameRate'),
                   lambda: project.GetSetting('timelineFrameRate')):
        try:
            v = float(getter())
            if v > 0:
                return v
        except Exception:
            pass
    return 24.0


def pick_list_gui(lists, current_video):
    """
    Shows a small dropdown picker (Fusion UIManager) to choose a cut list.
    Returns the chosen path, or None if cancelled / no GUI available.
    """
    bmd = globals().get('bmd')
    fusion = globals().get('fusion')
    if fusion is None:
        resolve = get_resolve()
        fusion = resolve.Fusion() if resolve else None
    if bmd is None or fusion is None:
        return None
    try:
        ui = fusion.UIManager
        disp = bmd.UIDispatcher(ui)
        names = [os.path.basename(p) for p in lists]
        vlabel = os.path.basename(current_video) if current_video else '(unknown)'
        win = disp.AddWindow(
            {'WindowTitle': 'ytsubtran — choose insert list', 'ID': 'win',
             'Geometry': [200, 200, 560, 150]},
            ui.VGroup([
                ui.Label({'Text': f'Open timeline: {vlabel}\n'
                                  'It does not match a known list. Pick one to mark '
                                  'the CURRENT timeline:'}),
                ui.ComboBox({'ID': 'combo'}),
                ui.HGroup([
                    ui.Button({'ID': 'ok', 'Text': 'Add markers'}),
                    ui.Button({'ID': 'cancel', 'Text': 'Cancel'}),
                ]),
            ]))
        items = win.GetItems()
        for name in names:
            items['combo'].AddItem(name)
        chosen = {'idx': None}

        def _ok(ev):
            chosen['idx'] = items['combo'].CurrentIndex
            disp.ExitLoop()

        def _cancel(ev):
            disp.ExitLoop()

        win.On.ok.Clicked = _ok
        win.On.cancel.Clicked = _cancel
        win.On.win.Close = _cancel
        win.Show()
        disp.RunLoop()
        win.Hide()
        return lists[chosen['idx']] if chosen['idx'] is not None else None
    except Exception:
        return None


def add_markers(timeline, segments, fps):
    """Adds a marker per segment; nudges by a frame if one already exists there."""
    added = 0
    for s, e, reason in segments:
        frame = seconds_to_frame(s, fps)
        duration = max(1, seconds_to_frame(e, fps) - frame) if MARKER_SPAN else 1
        note = reason or 'insert'
        for f in (frame, frame + 1, frame + 2):
            if timeline.AddMarker(f, MARKER_COLOR, 'Insert', note, duration, ''):
                added += 1
                break
    return added


def main():
    resolve = get_resolve()
    if resolve is None:
        print('✗ Could not connect to DaVinci Resolve. Run this from '
              'Workspace → Scripts inside Resolve.')
        return

    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        print('✗ No project open. Open your project first.')
        return
    timeline = project.GetCurrentTimeline()
    if not timeline:
        print('✗ No timeline open. Open the video timeline on the Edit page first.')
        return

    src_files = timeline_video_files(timeline)
    src = src_files[0] if src_files else None
    folder = SCAN_FOLDER or (os.path.dirname(src) if src else None)
    lists = sorted(glob.glob(os.path.join(folder, '*_inserts.txt'))) if folder else []

    cutlist = CUTLIST or list_for_video(src, lists)
    if not cutlist:
        if not lists:
            print('✗ No "*_inserts.txt" cut list found next to the timeline video.')
            print('  Run:  ytsubtran --file "<video>" --smart-inserts   (or --find-inserts)')
            print('  ...then run this script again, or set CUTLIST at the top of the file.')
            return
        print('⚠️  The open timeline does not match a known cut list. Found:')
        for i, p in enumerate(lists, 1):
            print(f'   {i}. {os.path.basename(p)}')
        cutlist = pick_list_gui(lists, src)
        if not cutlist:
            print('   No selection made. Open the matching video as the current '
                  'timeline, or set CUTLIST at the top of this script, then re-run.')
            return

    segments = load_cut_list(cutlist)
    if not segments:
        print(f'✗ Cut list is empty: {cutlist}')
        return

    # Communicate any mismatch but still let the user proceed on the current timeline.
    list_name = os.path.basename(cutlist)
    if src:
        vbase = os.path.splitext(os.path.basename(src))[0]
        if not list_name.startswith(vbase):
            print(f'⚠️  Note: current timeline video "{vbase}" does not match list '
                  f'"{list_name}". Markers go on the CURRENT timeline — make sure '
                  'it is the right video.')

    fps = get_fps(project, timeline)
    added = add_markers(timeline, segments, fps)
    print(f'✓ Added {added}/{len(segments)} "{MARKER_COLOR}" markers on '
          f'"{timeline.GetName()}" (from {list_name}, {fps:g} fps).')


if __name__ == '__main__':
    main()
