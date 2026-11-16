#!/usr/bin/env python3
"""Package only a newly rendered clay animation and non-sensitive QA counts."""
import argparse, hashlib, json, subprocess
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--frames', required=True)
    parser.add_argument('--pose', required=True)
    parser.add_argument('--output', required=True)
    args=parser.parse_args()
    frames=Path(args.frames); output=Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    pose=json.loads(Path(args.pose).read_text())
    fps=pose['fps']; duration=pose['duration']; expected=round(fps*duration)
    assert expected==144 and fps==24 and duration==6
    actual=list(frames.glob('frame-*.png'))
    assert len(actual)==expected, 'Rendered frame count mismatch'
    video=output/'clay-bizzyboom.mp4'
    subprocess.run(['ffmpeg','-nostdin','-y','-hide_banner','-loglevel','error','-framerate',str(fps),'-i',str(frames/'frame-%06d.png'),'-an','-c:v','libx264','-preset','medium','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(video)],check=True)
    probe=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration','-of','json',str(video)],check=True,capture_output=True,text=True)
    info=json.loads(probe.stdout)
    stream=info['streams'][0]
    assert stream['width']==576 and stream['height']==1024 and abs(float(info['format']['duration'])-6)<.06
    from PIL import Image, ImageDraw
    sample=[0,18,36,54,72,90,108,126]
    sheet=Image.new('RGB',(1152,1104),(235,237,240))
    draw=ImageDraw.Draw(sheet)
    for index,frame in enumerate(sample):
        img=Image.open(frames/f'frame-{frame:06d}.png').convert('RGB').resize((288,512))
        x=(index%4)*288; y=(index//4)*552
        sheet.paste(img,(x,y+30));draw.text((x+12,y+9),f'{frame/fps:.2f}s',fill=(25,30,40))
    sheet.save(output/'clay-contact-sheet.jpg',quality=92)
    quality=pose.get('quality',{})
    summary={'status':'rendered_for_review','duration':6,'fps':24,'frames':expected,'width':576,'height':1024,'codec':stream['codec_name'],'bytes':video.stat().st_size,'sha256':hashlib.sha256(video.read_bytes()).hexdigest(),'source_pixels_included':False,'audio_included':False,'pose_quality':quality}
    (output/'clay-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    render_summary=frames/'render-summary.json'
    if render_summary.exists(): (output/'render-summary.json').write_text(render_summary.read_text())
    print(json.dumps(summary),flush=True)
    return 0
if __name__=='__main__':
    raise SystemExit(main())
