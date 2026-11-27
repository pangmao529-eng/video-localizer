"""Stateless mechanical all-text layouts and two-part assembly.

No model, OCR, translation, credentials, store or Processor is used here. Callers
provide approved text, geometry, colours and font sizing evidence. Font sizes
are fixed; fitting may add line breaks but never shrinks or rewrites text.
"""
from fractions import Fraction
from collections import deque
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import subprocess
import tempfile


class OverlayError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise OverlayError(code)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _text(value):
    require(isinstance(value, str) and 1 <= len(value) <= 2000 and value.strip()
            and not any(c in value for c in '{}\\\r\t')
            and not any(ord(c) < 32 and c != '\n' for c in value), 'invalid_literal_text')
    return value


def _box(box, width, height):
    require(isinstance(box, dict) and set(box) == {'x', 'y', 'width', 'height'}
        and all(type(v) is int for v in box.values())
        and 0 <= box['x'] < width and 0 <= box['y'] < height
        and 0 < box['width'] <= width - box['x']
        and 0 < box['height'] <= height - box['y'], 'invalid_box')
    return dict(box)


def _overlap(a, b):
    return (max(a['x'], b['x']) < min(a['x']+a['width'], b['x']+b['width'])
            and max(a['y'], b['y']) < min(a['y']+a['height'], b['y']+b['height']))


def _fully_occluded(row):
    box,mask=row['layout_box'],row.get('occlusion_box')
    return mask is not None and mask['x']<=box['x'] and mask['y']<=box['y'] and \
        mask['x']+mask['width']>=box['x']+box['width'] and \
        mask['y']+mask['height']>=box['y']+box['height']


def _wrap(text, available, size, measure_width):
    """Retain whitespace and all reference characters; only insert newlines."""
    require('\n' not in text, 'preformatted_text_requires_separate_layout')
    lines, remaining = [], text
    while remaining:
        if measure_width(remaining, size) <= available:
            lines.append(remaining)
            break
        low, high = 0, len(remaining)
        while low < high:
            middle = (low+high+1)//2
            if measure_width(remaining[:middle], size) <= available:
                low = middle
            else:
                high = middle-1
        require(low > 0, 'text_does_not_fit_same_size')
        boundaries = [m.end() for m in re.finditer(r' +', remaining[:low])]
        at = boundaries[-1] if boundaries else low
        lines.append(remaining[:at])
        remaining = remaining[at:]
    require(''.join(lines) == text, 'target_changed')
    return lines


def prepare_plan(cues, *, width, height, fps_ratio, frame_count,
                 measure_width, line_height, font_name='Noto Sans CJK KR', measure_block=None):
    """Return validated per-cue fixed-size layouts.

    ``measure_width(text, font_size)`` and ``line_height(font_size)`` must be
    measured for the actual server font renderer. They are not OCR/inference.
    ``font_size`` is ASS em size, not the original video's visible glyph height.
    A single-frame OCR record (start == end) becomes exactly one frame.
    ``erase_box`` controls the clean-video patch, ``layout_box`` controls text.
    Explicit newlines are currently rejected; this function inserts safe ones.
    """
    require(type(width) is int and type(height) is int and 0 < width <= 3840
            and 0 < height <= 3840 and width % 2 == height % 2 == 0, 'invalid_dimensions')
    try:
        fps = Fraction(fps_ratio)
    except (ValueError, TypeError, ZeroDivisionError):
        raise OverlayError('invalid_fps') from None
    require(1 <= fps <= 60 and type(frame_count) is int and 0 < frame_count <= 18000, 'invalid_timeline')
    require(isinstance(font_name, str) and re.fullmatch(r'[A-Za-z0-9 -]{1,80}', font_name), 'invalid_font')
    require(isinstance(cues, list) and 1 <= len(cues) <= 1000, 'invalid_cues')
    require(callable(measure_width) and callable(line_height), 'font_metrics_required')
    duration = frame_count / float(fps)
    rows = []
    for index, cue in enumerate(cues):
        require(isinstance(cue, dict) and {'start','end','text','erase_box','layout_box','font_size'} <= set(cue)
                and set(cue) <= {'start','end','text','erase_box','layout_box','font_size',
                                'align','color','outline','outline_color','sizing_evidence','layer','occlusion_box'}, 'invalid_cue')
        require(_number(cue['start']) and _number(cue['end'])
                and 0 <= cue['start'] <= cue['end'] <= duration and cue['start'] < duration, 'invalid_time')
        first = math.floor(cue['start']*float(fps)+1e-7)
        end = min(frame_count, max(first+1, math.ceil(cue['end']*float(fps)-1e-7)))
        text = _text(cue['text'])
        erase, layout = (_box(cue[k], width, height) for k in ('erase_box','layout_box'))
        occlusion=_box(cue['occlusion_box'],width,height) if 'occlusion_box' in cue else None
        size, outline = cue['font_size'], cue.get('outline', 2)
        require(type(size) is int and 4 <= size <= 256, 'invalid_font_size')
        require(type(outline) is int and 0 <= outline <= 8, 'invalid_outline')
        align = cue.get('align', 'center')
        require(align in {'left','center','right'}, 'invalid_alignment')
        layer=cue.get('layer',0)
        if isinstance(layer,str): layer={'ui':0,'popup':1,'dialogue':2}.get(layer)
        require(type(layer) is int and 0<=layer<=10,'invalid_layer')
        color, border = cue.get('color','FFFFFF'), cue.get('outline_color','000000')
        require(all(isinstance(c,str) and re.fullmatch(r'[A-Fa-f0-9]{6}',c) for c in (color,border)), 'invalid_color')
        available = layout['width'] - 2*outline
        require(available > 0, 'text_does_not_fit_same_size')
        def measured(value, requested):
            number = measure_width(value, requested)
            require(_number(number) and number >= 0, 'invalid_font_metrics')
            return number
        lines = _wrap(text, available, size, measured)
        advance = line_height(size)
        require(_number(advance) and advance > 0, 'invalid_font_metrics')
        block_height=advance*len(lines)
        anchor_adjustment=0
        if measure_block is not None:
            require(callable(measure_block),'invalid_font_metrics')
            geometry=measure_block('\n'.join(lines),size)
            require(isinstance(geometry,dict) and all(_number(geometry.get(k)) for k in ('height','offset_y'))
                    and geometry['height']>0,'invalid_font_metrics')
            block_height=geometry['height']
            anchor_adjustment=-geometry['offset_y']
        require(math.ceil(block_height)+2*outline <= layout['height'], 'text_does_not_fit_same_size')
        row={'index':index,'start_frame':first,'end_frame':end,
            'start':first/float(fps),'end':end/float(fps),'text':text,'lines':lines,
            'erase_box':erase,'layout_box':layout,'font_size':size,
            'line_height':advance,'visible_block_height':block_height,
            'anchor_y_adjustment':anchor_adjustment,'align':align,'color':color.upper(),'layer':layer,
            'outline':outline,'outline_color':border.upper(),
            'zero_length_ocr_expanded':cue['start']==cue['end'],
            'sizing_evidence':cue.get('sizing_evidence','explicit_ass_font_size')}
        if occlusion is not None: row['occlusion_box']=occlusion
        rows.append(row)
    rows.sort(key=lambda row:(row['start_frame'],row['index']))
    for index, row in enumerate(rows):
        for other in rows[index+1:]:
            if other['start_frame'] >= row['end_frame']:
                break
            require(row['layer']!=other['layer'] or _fully_occluded(row) or _fully_occluded(other)
                    or not _overlap(row['layout_box'], other['layout_box']), 'overlapping_text_layouts')
    return {'schema':'all-text-overlay-v1','width':width,'height':height,
        'fps_ratio':str(fps),'frame_count':frame_count,'font_name':font_name,
        'font_size_policy':'fixed_no_shrink','cues':rows,'target_characters_preserved':True}


def _stamp(seconds):
    # ASS only resolves centiseconds. Floor both boundaries so a 30 fps
    # single-frame event at frame 2 (0.0666...) is visible on that frame.
    # At <=60 fps the previous frame is more than one centisecond earlier.
    ticks = int(math.floor(seconds*100+1e-7))
    return f'{ticks//360000}:{ticks//6000%60:02d}:{ticks//100%60:02d}.{ticks%100:02d}'


def _bgr(rgb):
    return rgb[4:6]+rgb[2:4]+rgb[0:2]


def serialize_ass(plan):
    """Use explicit placement and no automatic font scaling or smart wrapping."""
    width, height = plan['width'], plan['height']
    header = ('[Script Info]\nScriptType: v4.00+\n'
        f'PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n'
        '\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
        'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: Default,{plan["font_name"]},32,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,'
        '0,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1\n'
        '\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    output=[]
    for row in plan['cues']:
        b=row['layout_box']; outline=row['outline']
        alignment={'left':4,'center':5,'right':6}[row['align']]
        x={'left':b['x']+outline,'center':b['x']+b['width']/2,
           'right':b['x']+b['width']-outline}[row['align']]
        y=b['y']+b['height']/2+row.get('anchor_y_adjustment',0)
        tags=(f'{{\\an{alignment}\\pos({x:g},{y:g})\\fs{row["font_size"]}\\fscx100\\fscy100'
              f'\\bord{outline}\\shad0\\1c&H{_bgr(row["color"])}&\\3c&H{_bgr(row["outline_color"])}&\\q2}}')
        if row.get('occlusion_box') is not None:
            mask=row['occlusion_box']
            tags=tags[:-1]+f'\\iclip({mask["x"]},{mask["y"]},{mask["x"]+mask["width"]},{mask["y"]+mask["height"]})}}'
        literal=r'\N'.join(row['lines'])
        require(_stamp(row['start']) != _stamp(row['end']), 'ass_time_resolution')
        output.append(f'Dialogue: {row.get("layer",0)},{_stamp(row["start"])},{_stamp(row["end"])},Default,,0,0,0,,{tags}{literal}\n')
    return header+''.join(output)


def filter_graph(plan, *, ass_filename='subtitles.ass'):
    """Clean patches affect only supplied regions and integer frame intervals."""
    require(ass_filename == 'subtitles.ass', 'fixed_ass_filename_required')
    rows=plan['cues']; require(bool(rows),'invalid_cues')
    graph=['[0:v]setpts=PTS-STARTPTS[base0]']
    graph.append('[1:v]setpts=PTS-STARTPTS,split='+str(len(rows))+''.join(f'[clean{i}]' for i in range(len(rows))))
    for index,row in enumerate(rows):
        b=row['erase_box']; x,y=b['x']//2*2,b['y']//2*2
        right=min(plan['width'],math.ceil((b['x']+b['width'])/2)*2)
        bottom=min(plan['height'],math.ceil((b['y']+b['height'])/2)*2)
        graph.append(f'[clean{index}]crop={right-x}:{bottom-y}:{x}:{y}[patch{index}]')
        graph.append(f'[base{index}][patch{index}]overlay=x={x}:y={y}:eof_action=pass:'
                     f"enable='gte(n,{row['start_frame']})*lt(n,{row['end_frame']})'[base{index+1}]")
    graph.append(f'[base{len(rows)}]ass=filename={ass_filename}[out]')
    return ';'.join(graph)


def _run(command, *, cwd=None, timeout=180, max_output=256*1024**2):
    result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,cwd=cwd,timeout=timeout,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    require(result.returncode==0, 'media_command_failed')
    require(len(result.stdout)<=max_output,'media_output_too_large')
    return result.stdout


def measure_texts(ffmpeg, texts, font_size, *, font_name='Noto Sans CJK KR',
                  canvas_width=2048, canvas_height=512):
    """Measure actual libass visible ink for literal strings on synthetic frames.

    Run on the rendering server. Width is ink width, excluding trailing spaces;
    no inference is performed. Bounds are measured before an outline is added.
    Returns [{text,width,height,left,top,font_size}, ...]. Empty text is forbidden.
    """
    require(isinstance(texts,list) and 1<=len(texts)<=100, 'invalid_metric_inputs')
    require(type(font_size) is int and 4<=font_size<=256, 'invalid_font_size')
    require(re.fullmatch(r'[A-Za-z0-9 -]{1,80}',font_name) is not None,'invalid_font')
    require(canvas_width in (512,1024,2048,4096) and canvas_height in (256,512,1024),'invalid_metric_canvas')
    for value in texts:
        _text(value)
    plan={'width':canvas_width,'height':canvas_height,'font_name':font_name,'cues':[]}
    for index,value in enumerate(texts):
        plan['cues'].append({'start':index,'end':index+1,'font_size':font_size,
            'layout_box':{'x':32,'y':32,'width':canvas_width-64,'height':canvas_height-64},
            'align':'left','outline':0,'color':'FFFFFF','outline_color':'000000','lines':value.split('\n')})
    with tempfile.TemporaryDirectory(prefix='text-metrics-') as temporary:
        folder=Path(temporary)
        (folder/'subtitles.ass').write_text(serialize_ass(plan),encoding='utf-8')
        raw=_run([str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-threads','2',
            '-filter_threads','1','-f','lavfi','-i',f'color=black:s={canvas_width}x{canvas_height}:r=1',
            '-vf','ass=filename=subtitles.ass,format=gray','-frames:v',str(len(texts)),
            '-f','rawvideo','pipe:1'],cwd=folder)
    stride=canvas_width*canvas_height
    require(len(raw)==stride*len(texts),'metric_frame_count')
    results=[]
    for index,value in enumerate(texts):
        frame=raw[index*stride:(index+1)*stride]
        xs=[]; ys=[]
        for y in range(canvas_height):
            line=frame[y*canvas_width:(y+1)*canvas_width]
            hit=[x for x,v in enumerate(line) if v>32]
            if hit: xs.extend((hit[0],hit[-1]));ys.append(y)
        require(bool(xs),'missing_font_glyphs')
        require(max(xs)<canvas_width-2 and min(xs)>1 and max(ys)<canvas_height-2 and min(ys)>1,'metric_text_clipped')
        results.append({'text':value,'width':max(xs)-min(xs)+1,'height':max(ys)-min(ys)+1,
            'left':min(xs),'top':min(ys),'font_size':font_size})
    return results


class AssFontMetrics:
    """Lazy measured geometry for prepare_plan, using the exact libass font."""
    def __init__(self, ffmpeg, font_name='Noto Sans CJK KR'):
        self.ffmpeg,self.font_name=ffmpeg,font_name
        self.cache={}
        self.advances={}

    def measure(self,text,size):
        key=(text,size)
        if key not in self.cache:
            self.cache[key]=measure_texts(self.ffmpeg,[text],size,font_name=self.font_name)[0]
        return self.cache[key]

    def width(self,text,size):
        # A layout prefix consisting solely of whitespace is visually empty.
        return self.measure(text,size)['width'] if text.strip() else 0

    def line_height(self,size):
        if size not in self.advances:
            values=measure_texts(self.ffmpeg,['가','가\n가'],size,font_name=self.font_name)
            advance=values[1]['height']-values[0]['height']
            require(advance>0,'invalid_font_metrics')
            # A whole line box is a conservative allowance for a single line.
            self.advances[size]=max(advance,values[0]['height'])
        return self.advances[size]

    def block(self,text,size):
        value=self.measure(text,size)
        # measure_texts uses a 512-pixel canvas and places the logical ASS
        # anchor at y=256. Compensate its font-specific visible-ink offset.
        return {'height':value['height'],'offset_y':value['top']+value['height']/2-256}


def calibrate_font_size(ffmpeg, text, visible_ink_height, *, font_name='Noto Sans CJK KR'):
    """Match caller-measured visible height, not an assumed OCR/font equivalence."""
    require(type(visible_ink_height) is int and 4<=visible_ink_height<=160,'invalid_visible_height')
    low,high=4,min(256,visible_ink_height*3+8); measured={}
    while low<=high:
        size=(low+high)//2
        metric=measure_texts(ffmpeg,[text],size,font_name=font_name,canvas_width=4096,canvas_height=1024)[0]
        measured[size]=metric
        if metric['height']<visible_ink_height: low=size+1
        elif metric['height']>visible_ink_height: high=size-1
        else: break
    size=min(measured,key=lambda n:(abs(measured[n]['height']-visible_ink_height),n))
    return {'font_size':size,'visible_ink_height':measured[size]['height'],
        'requested_visible_ink_height':visible_ink_height,
        'difference_pixels':measured[size]['height']-visible_ink_height,
        'basis':'caller_measured_visible_ink_height','font_name':font_name}


def _ppm_rgb(data):
    """Decode FFmpeg's bounded P6 output without an imaging dependency."""
    tokens=[]; position=0
    while len(tokens)<4:
        while position<len(data) and data[position] in b' \t\r\n': position+=1
        if position<len(data) and data[position]==35:
            stop=data.find(b'\n',position)
            require(stop>=0,'invalid_source_frame')
            position=stop+1
            continue
        end=position
        while end<len(data) and data[end] not in b' \t\r\n': end+=1
        require(end>position,'source_frame_not_found')
        tokens.append(data[position:end]);position=end
    require(tokens[0]==b'P6' and tokens[3]==b'255','invalid_source_frame')
    try: width,height=int(tokens[1]),int(tokens[2])
    except ValueError: raise OverlayError('invalid_source_frame') from None
    require(0<width<=3840 and 0<height<=3840,'source_frame_dimensions')
    require(position<len(data) and data[position] in b' \t\r\n','invalid_source_frame')
    position += 2 if data[position:position+2]==b'\r\n' else 1
    require(len(data)-position==width*height*3,'invalid_source_frame_size')
    return width,height,memoryview(data)[position:]


def _source_ink_geometry(rgb,width,height,target):
    """Geometric colour segmentation only: never identify or OCR characters."""
    require(len(rgb)==width*height*3,'invalid_source_crop')
    pixels=width*height
    loose=bytearray(pixels);core=bytearray(pixels)
    squared=sum(value*value for value in target)
    dark=max(target)<=48
    for index in range(pixels):
        r,g,b=rgb[index*3:index*3+3]
        if dark:
            # Black type has dark body with antialiasing towards its outline or
            # backing. Saturated dark scenery is not a neutral black letter.
            distance=max(abs(r-target[0]),abs(g-target[1]),abs(b-target[2]))
            neutral=max(r,g,b)-min(r,g,b)<=60
            loose[index]=neutral and distance<=145
            core[index]=neutral and distance<=55
        else:
            # Match target hue down to visible antialias pixels. An inner core
            # is mandatory, so a similarly coloured dark background is not ink.
            alpha=(r*target[0]+g*target[1]+b*target[2])/squared
            residual=max(abs(r-alpha*target[0]),abs(g-alpha*target[1]),abs(b-alpha*target[2]))
            loose[index]=.12<=alpha<=1.26 and residual<=38
            core[index]=.70<=alpha<=1.26 and residual<=48
    initial_count=sum(loose)
    require(initial_count>=6 and sum(core)>=3,'source_ink_not_found')
    accepted=[]; rejected={'background':0,'weak_or_tiny':0}
    for seed in range(pixels):
        if not loose[seed]: continue
        queue=deque([seed]);loose[seed]=0
        xs=[];ys=[];members=[];count=strong=0
        while queue:
            index=queue.popleft();y,x=divmod(index,width)
            xs.append(x);ys.append(y);count+=1;strong+=core[index]
            if len(members)<50000: members.append(index)
            for yy in range(max(0,y-1),min(height,y+2)):
                for xx in range(max(0,x-1),min(width,x+2)):
                    neighbour=yy*width+xx
                    if loose[neighbour]: loose[neighbour]=0;queue.append(neighbour)
        x0,x1,y0,y1=min(xs),max(xs),min(ys),max(ys)
        bw,bh=x1-x0+1,y1-y0+1
        touches=x0==0 or x1==width-1 or y0==0 or y1==height-1
        filling=count/(bw*bh)
        background=(count>=min(50000,pixels*.27)
            or (touches and (bw>=width*.35 or bh>=height*.80))
            or (bw>=width*.8 and bh>=height*.6)
            or (filling>.78 and bw>height*.8 and bh>height*.45))
        if background:
            rejected['background']+=1
            continue
        if count<2 or strong<1 or strong/count<.04 or bh<2:
            rejected['weak_or_tiny']+=1
            continue
        accepted.append({'pixels':members,'count':count,'core':strong,
            'box':[x0,y0,x1+1,y1+1],'touches':touches})
    require(bool(accepted),'source_ink_not_isolated')
    rows=[0]*height; strong_rows=[0]*height
    for component in accepted:
        for index in component['pixels']:
            y=index//width;rows[y]+=1;strong_rows[y]+=core[index]
    active=[y for y,count in enumerate(rows) if count]
    groups=[]
    for y in active:
        # Small disconnected strokes belong to the same printed line. Large
        # gaps imply multiple lines or residual background, not a font size.
        if not groups or y-groups[-1][-1]>max(3,round(height*.08)):
            groups.append([y])
        else: groups[-1].append(y)
    scored=sorted(((sum(strong_rows[y] for y in group),group) for group in groups),reverse=True)
    require(bool(scored) and scored[0][0]>=3,'source_ink_not_found')
    if len(scored)>1:
        require(scored[1][0]<scored[0][0]*.30,'source_ink_ambiguous_bands')
    band=scored[0][1];top,bottom=band[0],band[-1]+1
    contributing=[component for component in accepted
                  if component['box'][1]<bottom and component['box'][3]>top]
    xs=[index%width for component in contributing for index in component['pixels']
        if top<=index//width<bottom]
    require(xs and 4<=bottom-top<=160,'source_ink_height_unreliable')
    require(top>0 and bottom<height,'source_ink_clipped')
    left,right=min(xs),max(xs)+1
    require(right-left>=max(4,(bottom-top)*.45),'source_ink_height_unreliable')
    if len(contributing)==1:
        component=contributing[0]
        require(component['count']>=8 and component['core']>=4,'source_ink_too_sparse')
    total_core=sum(strong_rows)
    dominance=scored[0][0]/max(1,total_core)
    require(dominance>=.60,'source_ink_ambiguous_bands')
    confidence='high' if len(contributing)>=4 and dominance>=.85 else 'medium'
    return {'height':bottom-top,'bbox':[left,top,right,bottom], 'confidence':confidence,
        'diagnostic':{'candidate_pixels':initial_count,'retained_components':len(accepted),
            'line_components':len(contributing),'rejected_components':rejected,
            'line_core_pixels':scored[0][0],'dominant_line_core_fraction':round(dominance,6),
            'color_mode':'dark_neutral' if dark else 'target_hue_and_luminance',
            'measurement':'visible_colored_ink_excluding_outline',
            'confidence_kind':'geometric_heuristic_not_character_recognition'}}


def measure_source_ink_height(ffmpeg, source, frame, box, color='FFFFFF'):
    """Measure a specified source text region physically, without OCR.

    Execute on the video-processing server. A supplied RGB colour selects the
    glyph body; 8-connected components and row projections reject large backing
    regions. Failure/ambiguity is explicit and never substitutes the OCR-box
    height. This estimates visible coloured ink, not font metadata or point size.
    """
    source=_file(source)
    require(type(frame) is int and 0<=frame<36000,'invalid_source_frame_index')
    require(isinstance(color,str) and re.fullmatch(r'[A-Fa-f0-9]{6}',color),'invalid_source_ink_color')
    require(isinstance(box,dict) and set(box)=={'x','y','width','height'}
        and all(type(v) is int for v in box.values())
        and 0<=box['x']<3840 and 0<=box['y']<3840 and 0<box['width']<=3840
        and 0<box['height']<=1024 and box['width']*box['height']<=2*1024**2,'invalid_source_ink_box')
    # Decode a complete frame as PPM first, so crop coordinates are checked
    # against its real dimensions instead of accepting FFmpeg crop clamping.
    data=_run([str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-threads','2',
        '-filter_threads','1','-protocol_whitelist','file,pipe','-noautorotate','-i',str(source),
        '-vf',f"select='eq(n,{frame})',format=rgb24",'-fps_mode','passthrough',
        '-frames:v','1','-c:v','ppm','-f','image2pipe','pipe:1'],max_output=48*1024**2)
    width,height,full=_ppm_rgb(data)
    _box(box,width,height)
    rgb=bytearray()
    for y in range(box['y'],box['y']+box['height']):
        first=(y*width+box['x'])*3
        rgb.extend(full[first:first+box['width']*3])
    target=tuple(int(color[i:i+2],16) for i in (0,2,4))
    result=_source_ink_geometry(rgb,box['width'],box['height'],target)
    left,top,right,bottom=result.pop('bbox')
    result.update(frame=frame,color=color.upper(),source_box=dict(box),
        ink_box={'x':box['x']+left,'y':box['y']+top,'width':right-left,'height':bottom-top})
    return result


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def _file(path):
    path=Path(path).resolve()
    value=path.stat()
    require(stat.S_ISREG(value.st_mode) and not Path(path).is_symlink()
        and 12<=value.st_size<=200*1024**2,'invalid_media_file')
    return path


def _probe(ffprobe,path):
    value=json.loads(_run([str(ffprobe),'-v','error','-threads','2','-count_frames','-show_streams',
        '-show_data_hash','sha256','-of','json',str(path)],max_output=4*1024**2))
    videos=[s for s in value['streams'] if s['codec_type']=='video']
    audios=[s for s in value['streams'] if s['codec_type']=='audio']
    require(len(videos)==1 and len(audios)<=1,'invalid_media_streams')
    return videos[0], audios[0] if audios else None


def _stream_hash(ffmpeg,path,selector):
    output=_run([str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error',
        '-protocol_whitelist','file,pipe','-i',str(path),'-map',selector,'-c','copy',
        '-f','hash','-hash','sha256','pipe:1'],max_output=1024)
    return output.decode().strip()


def _cfr(ffprobe,path,video):
    fps=float(Fraction(video['avg_frame_rate']))
    require(1<=fps<=120 and abs(float(video.get('start_time','nan')))<=.00001,'invalid_frame_timeline')
    value=json.loads(_run([str(ffprobe),'-v','error','-select_streams','v:0',
        '-show_entries','frame=best_effort_timestamp_time','-of','json',str(path)],max_output=4*1024**2))
    times=[float(r['best_effort_timestamp_time']) for r in value['frames']]
    require(len(times)==int(video['nb_read_frames']) and
        all(abs(t-i/fps)<.0001 for i,t in enumerate(times)),'non_cfr_media')


def join_two_parts(first, second, original, output, ffmpeg, ffprobe):
    """Stream-copy two video parts and the entire original audio.

    Inputs are read-only. This writes the requested new output and scratch files
    in its parent. No Processor/store is initialized; no existing output is
    overwritten. Audio is deliberately not cut to video duration, preserving a
    source tail such as the 58 ms AAC ending in the 30 s test original.
    """
    paths=[_file(first),_file(second)]
    source=_file(original); destination=Path(output).resolve()
    require(destination not in paths+[source] and not destination.exists()
            and destination.parent.is_dir(),'new_output_required')
    identities={p:_digest(p) for p in paths+[source]}
    original_video,original_audio=_probe(ffprobe,source)
    _cfr(ffprobe,source,original_video)
    data=[_probe(ffprobe,p) for p in paths]
    keys=('codec_name','profile','level','width','height','pix_fmt','time_base',
          'sample_aspect_ratio','field_order','extradata_hash','avg_frame_rate')
    signature=tuple(data[0][0].get(k) for k in keys)
    require(data[0][0]['codec_name'] in ('h264','hevc') and data[0][0].get('extradata_hash')
            and all(tuple(v.get(k) for k in keys)==signature for v,_ in data),'incompatible_video_parts')
    count=sum(int(v['nb_read_frames']) for v,_ in data)
    require(count==int(original_video['nb_read_frames']) and all(v['width']==original_video['width']
        and v['height']==original_video['height'] and v['avg_frame_rate']==original_video['avg_frame_rate']
        for v,_ in data),'part_source_mismatch')
    with tempfile.TemporaryDirectory(prefix='join-two-',dir=destination.parent) as temporary:
        folder=Path(temporary); stripped=[]
        for index,(path,(video,audio)) in enumerate(zip(paths,data)):
            _cfr(ffprobe,path,video)
            part=folder/f'part-{index}.mp4'
            _run([str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-n','-threads','2',
                '-protocol_whitelist','file,pipe','-i',str(path),'-copyts','-map','0:v:0','-an',
                '-c:v','copy','-avoid_negative_ts','disabled','-map_metadata','-1','-movflags','+faststart',str(part)])
            require(_stream_hash(ffmpeg,path,'0:v:0')==_stream_hash(ffmpeg,part,'0:v:0'),'video_copy_changed')
            stripped.append(part)
        listing=['ffconcat version 1.0']
        for index,(video,_) in enumerate(data):
            listing += [f"file 'part-{index}.mp4'", f"duration {int(video['nb_read_frames'])/float(Fraction(video['avg_frame_rate'])):.12f}"]
        (folder/'inputs.ffconcat').write_text('\n'.join(listing)+'\n',encoding='utf-8')
        candidate=folder/'joined.mp4'
        _run([str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-n','-threads','2',
            '-protocol_whitelist','file,pipe','-f','concat','-safe','1','-i','inputs.ffconcat',
            '-protocol_whitelist','file,pipe','-i',str(source),'-map','0:v:0','-map','1:a:0?',
            '-c:v','copy','-c:a','copy','-avoid_negative_ts','disabled','-map_metadata','-1',
            '-movflags','+faststart',str(candidate)],cwd=folder)
        video,audio=_probe(ffprobe,candidate)
        _cfr(ffprobe,candidate,video)
        _run([str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-threads','2',
            '-i',str(candidate),'-f','null','-'],max_output=1024)
        audio_ok=(audio is None)==(original_audio is None)
        if audio_ok and original_audio:
            audio_ok=(audio['codec_name']==original_audio['codec_name']
                and abs(float(audio['start_time'])-float(original_audio['start_time']))<=.003
                and abs(float(audio['duration'])-float(original_audio['duration']))<=.003
                and _stream_hash(ffmpeg,candidate,'0:a:0')==_stream_hash(ffmpeg,source,'0:a:0'))
        checks={'frame_count_preserved':int(video['nb_read_frames'])==count,
            'frame_rate_preserved':video['avg_frame_rate']==original_video['avg_frame_rate'],
            'dimensions_preserved':all(video[k]==original_video[k] for k in ('width','height')),
            'video_duration_preserved':abs(float(video['duration'])-float(original_video['duration']))<=.0001,
            'entire_original_audio_preserved':audio_ok,
            'inputs_unchanged':all(_digest(p)==sha for p,sha in identities.items()),'full_decode':True}
        require(all(checks.values()),'joined_media_validation_failed')
        candidate.replace(destination)
    return {'status':'passed','output':str(destination),'sha256':_digest(destination),
        'bytes':destination.stat().st_size,'frame_count':count,'fps_ratio':video['avg_frame_rate'],
        'video_duration':float(video['duration']),
        'audio_duration':float(audio['duration']) if audio else None,
        'video_reencoded_during_join':False,'checks':checks}
