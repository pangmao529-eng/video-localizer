"""Pure layout and isolated synthetic FFmpeg tests, never real user footage."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('text_overlay_render',Path(__file__).resolve().parents[1]/'server/text_overlay_render.py')
overlay=importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


def cue(**updates):
    value={'start':0,'end':1,'text':'한글','erase_box':{'x':20,'y':30,'width':80,'height':40},
        'layout_box':{'x':20,'y':20,'width':160,'height':60},'font_size':20}
    value.update(updates)
    return value


def plan(cues):
    return overlay.prepare_plan(cues,width=640,height=360,fps_ratio='30/1',frame_count=60,
        measure_width=lambda text,size:len(text)*size*.5,line_height=lambda size:size*1.2)


class LayoutTests(unittest.TestCase):
    def test_simultaneous_ui_has_independent_font_position_and_colour(self):
        value=plan([cue(),cue(font_size=32,color='FFAA00',outline=1,align='right',
            layout_box={'x':300,'y':200,'width':250,'height':60},erase_box={'x':310,'y':200,'width':100,'height':40})])
        script=overlay.serialize_ass(value)
        self.assertEqual([r['font_size'] for r in value['cues']],[20,32])
        self.assertIn(r'\fs20\fscx100\fscy100',script)
        self.assertIn(r'\fs32\fscx100\fscy100',script)
        self.assertIn(r'\an6\pos(549,230)',script)
        self.assertIn(r'\1c&H00AAFF&',script)
        self.assertEqual(value['font_size_policy'],'fixed_no_shrink')

    def test_long_text_wraps_without_changing_font_or_reference(self):
        value=plan([cue(text='자원을 하나에만 몰아 먼저 끝까지 키워.',
            layout_box={'x':20,'y':20,'width':130,'height':110})])
        row=value['cues'][0]
        self.assertEqual(''.join(row['lines']),row['text'])
        self.assertGreater(len(row['lines']),1)
        self.assertEqual(row['font_size'],20)
        self.assertIn(r'\N',overlay.serialize_ass(value))

    def test_insufficient_layout_is_rejected_instead_of_shrinking(self):
        with self.assertRaisesRegex(overlay.OverlayError,'text_does_not_fit_same_size'):
            plan([cue(text='고유 글자 그대로',layout_box={'x':20,'y':20,'width':20,'height':25})])

    def test_measured_ink_centres_in_tight_ui_box_without_shrinking_font(self):
        value=overlay.prepare_plan([cue(font_size=32,layout_box={'x':20,'y':20,'width':150,'height':28})],
            width=640,height=360,fps_ratio='30/1',frame_count=60,
            measure_width=lambda text,size:40,line_height=lambda size:36,
            measure_block=lambda text,size:{'height':22,'offset_y':6})
        self.assertEqual(value['cues'][0]['font_size'],32)
        self.assertEqual(value['cues'][0]['anchor_y_adjustment'],-6)
        self.assertIn(r'\pos(95,28)',overlay.serialize_ass(value))

    def test_overlapping_ui_layouts_rejected_but_sequential_layout_allowed(self):
        with self.assertRaisesRegex(overlay.OverlayError,'overlapping_text_layouts'):
            plan([cue(),cue(start=.5,end=1.5)])
        self.assertEqual(len(plan([cue(),cue(start=1,end=2)])['cues']),2)

    def test_explicit_cloud_layers_authorize_popup_dialogue_overlap(self):
        value=plan([cue(layer='ui'),cue(layer='popup'),cue(layer='dialogue')])
        self.assertEqual([r['layer'] for r in value['cues']],[0,1,2])
        script=overlay.serialize_ass(value)
        for layer in (0,1,2): self.assertIn(f'Dialogue: {layer},',script)
        with self.assertRaisesRegex(overlay.OverlayError,'invalid_layer'):
            plan([cue(layer='random')])

    def test_popup_occlusion_is_literal_iclip_and_does_not_change_patch(self):
        mask={'x':10,'y':10,'width':300,'height':100}
        value=plan([cue(occlusion_box=mask),cue()])
        self.assertEqual(value['cues'][0]['occlusion_box'],mask)
        self.assertIn(r'\iclip(10,10,310,110)',overlay.serialize_ass(value))
        self.assertEqual(value['cues'][0]['erase_box'],cue()['erase_box'])
        with self.assertRaisesRegex(overlay.OverlayError,'overlapping_text_layouts'):
            plan([cue(occlusion_box={'x':20,'y':20,'width':10,'height':10}),cue()])
        with self.assertRaisesRegex(overlay.OverlayError,'invalid_box'):
            plan([cue(occlusion_box={'x':630,'y':20,'width':30,'height':10})])

    def test_zero_length_ocr_is_one_frame_with_visible_ass_interval(self):
        value=plan([cue(start=2/30,end=2/30)])
        row=value['cues'][0]
        self.assertEqual((row['start_frame'],row['end_frame']),(2,3))
        self.assertTrue(row['zero_length_ocr_expanded'])
        self.assertIn('0:00:00.06,0:00:00.10',overlay.serialize_ass(value))
        self.assertIn("gte(n,2)*lt(n,3)",overlay.filter_graph(value))

    def test_roi_crop_is_separate_and_outward_even_aligned(self):
        value=plan([cue(erase_box={'x':21,'y':31,'width':81,'height':41})])
        graph=overlay.filter_graph(value)
        self.assertIn('crop=82:42:20:30',graph)
        self.assertNotIn('crop=160:60',graph)
        self.assertIn('[base1]ass=filename=subtitles.ass[out]',graph)

    def test_ass_injection_invalid_geometry_or_font_size_rejected(self):
        for update in ({'text':r'{\fs1}한글'},{'text':'한글\n다음'}, {'font_size':0},
                       {'layout_box':{'x':630,'y':20,'width':50,'height':60}},
                       {'start':2,'end':2}, {'color':'FFFFFF&script'}):
            with self.subTest(update=update),self.assertRaises(overlay.OverlayError):
                plan([cue(**update)])


MEDIA=all(os.environ.get('VIDEO_LOCALIZER_TEST_'+key) for key in ('FFMPEG','FFPROBE'))


@unittest.skipUnless(MEDIA,'Set VIDEO_LOCALIZER_TEST_FFMPEG/FFPROBE')
class SyntheticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=os.environ['VIDEO_LOCALIZER_TEST_FFMPEG']
        cls.ffprobe=os.environ['VIDEO_LOCALIZER_TEST_FFPROBE']

    def run_media(self,args):
        result=subprocess.run([self.ffmpeg,'-nostdin','-hide_banner','-loglevel','error',*args],
            capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace')[-1000:])
        return result.stdout

    def test_real_font_metrics_fixed_size_and_visible_height_calibration(self):
        font='Noto Sans KR' if os.name=='nt' else 'Noto Sans CJK KR'
        small=overlay.measure_texts(self.ffmpeg,['한글'],16,font_name=font)[0]
        large=overlay.measure_texts(self.ffmpeg,['한글'],32,font_name=font)[0]
        self.assertGreater(small['height'],4)
        self.assertGreater(large['height'],small['height']*1.6)
        result=overlay.calibrate_font_size(self.ffmpeg,'한글',large['height'],font_name=font)
        self.assertLessEqual(abs(result['difference_pixels']),1)
        metrics=overlay.AssFontMetrics(self.ffmpeg,font)
        self.assertGreaterEqual(metrics.line_height(32),large['height'])
        value=overlay.prepare_plan([cue(font_size=32,layout_box={'x':20,'y':20,'width':250,'height':90})],
            width=640,height=360,fps_ratio='30/1',frame_count=60,
            measure_width=metrics.width,line_height=metrics.line_height,font_name=font)
        self.assertEqual(value['cues'][0]['font_size'],32)

    def test_join_preserves_all_original_audio_including_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            encode=['-c:v','libx264','-threads','2','-preset','veryfast','-crf','18','-pix_fmt','yuv420p']
            self.run_media(['-f','lavfi','-i','color=blue:s=160x90:r=30:d=2',
                '-f','lavfi','-i','sine=frequency=330:sample_rate=48000:duration=2.06',
                *encode,'-c:a','aac',str(root/'original.mp4')])
            for name,color in (('first','red'),('second','green')):
                self.run_media(['-f','lavfi','-i',f'color={color}:s=160x90:r=30:d=1',
                    *encode,'-an',str(root/(name+'.mp4'))])
            result=overlay.join_two_parts(root/'first.mp4',root/'second.mp4',root/'original.mp4',
                root/'joined.mp4',self.ffmpeg,self.ffprobe)
            self.assertEqual(result['frame_count'],60)
            self.assertEqual(result['video_duration'],2)
            self.assertGreater(result['audio_duration'],2.04)
            self.assertTrue(result['checks']['entire_original_audio_preserved'])
            self.assertFalse(result['video_reencoded_during_join'])
            with self.assertRaisesRegex(overlay.OverlayError,'new_output_required'):
                overlay.join_two_parts(root/'first.mp4',root/'second.mp4',root/'original.mp4',
                    root/'joined.mp4',self.ffmpeg,self.ffprobe)

    def test_zero_length_record_renders_text_and_patch_on_exactly_one_frame(self):
        font='Noto Sans KR' if os.name=='nt' else 'Noto Sans CJK KR'
        metrics=overlay.AssFontMetrics(self.ffmpeg,font)
        layout_height=metrics.measure('한글',20)['height']+4
        value=overlay.prepare_plan([cue(start=2/30,end=2/30,font_size=20,
            erase_box={'x':10,'y':150,'width':20,'height':20},
            layout_box={'x':40,'y':20,'width':240,'height':layout_height})],
            width=320,height=180,fps_ratio='30/1',frame_count=5,
            measure_width=metrics.width,line_height=metrics.line_height,measure_block=metrics.block,font_name=font)
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            (folder/'subtitles.ass').write_text(overlay.serialize_ass(value),encoding='utf-8')
            raw=overlay._run([self.ffmpeg,'-nostdin','-hide_banner','-loglevel','error',
                '-filter_complex_threads','1','-f','lavfi','-i','color=blue:s=320x180:r=30',
                '-f','lavfi','-i','color=red:s=320x180:r=30',
                '-filter_complex',overlay.filter_graph(value),'-map','[out]',
                '-frames:v','5','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],cwd=folder)
        stride=320*180*3
        frames=[raw[i*stride:(i+1)*stride] for i in range(5)]
        self.assertEqual(len(raw),stride*5)
        self.assertEqual(frames[0],frames[1])
        self.assertEqual(frames[0],frames[3])
        self.assertEqual(frames[0],frames[4])
        self.assertNotEqual(frames[0],frames[2])
        pixel=(155*320+15)*3
        self.assertGreater(frames[2][pixel],200)
        self.assertLess(frames[2][pixel+2],50)
        white=[]
        for y in range(180):
            for x in range(320):
                at=(y*320+x)*3
                if min(frames[2][at:at+3])>200: white.append((x,y))
        self.assertTrue(white,'The single-frame ASS cue must actually be visible')
        self.assertTrue(all(40<=x<280 and 20<=y<20+layout_height for x,y in white),
                        'All visible ink must remain inside the fixed-size UI layout box')

    def test_source_ink_measurement_white_gold_dark_with_background_block(self):
        font='Noto Sans KR' if os.name=='nt' else 'Noto Sans CJK KR'
        text='字幕测量'
        expected=overlay.measure_texts(self.ffmpeg,[text],32,font_name=font)[0]['height']
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            for color,background in (('FFFFFF','black'),('F5CF65','black'),('282828','white')):
                with self.subTest(color=color):
                    value={'width':640,'height':360,'font_name':font,'cues':[{
                        'start':0,'end':1,'font_size':32,
                        'layout_box':{'x':200,'y':160,'width':350,'height':70},
                        'align':'center','outline':0 if color=='282828' else 2,
                        'color':color,'outline_color':'000000','lines':[text]}]}
                    (folder/'subtitles.ass').write_text(overlay.serialize_ass(value),encoding='utf-8')
                    source=folder/(color+'.mkv')
                    overlay._run([self.ffmpeg,'-nostdin','-hide_banner','-loglevel','error',
                        '-filter_threads','1','-f','lavfi','-i',f'color={background}:s=640x360:r=30',
                        '-vf',f'drawbox=x=0:y=155:w=150:h=90:color=0x{color}:t=fill,ass=filename=subtitles.ass',
                        '-frames:v','3','-c:v','ffv1',str(source)],cwd=folder)
                    result=overlay.measure_source_ink_height(self.ffmpeg,source,1,
                        {'x':0,'y':155,'width':620,'height':90},color=color)
                    self.assertLessEqual(abs(result['height']-expected),2,result)
                    self.assertIn(result['confidence'],('high','medium'))
                    self.assertGreaterEqual(result['diagnostic']['rejected_components']['background'],1)
                    self.assertEqual(result['color'],color)
                    self.assertGreater(result['ink_box']['x'],150)
                    with self.assertRaises(overlay.OverlayError):
                        overlay.measure_source_ink_height(self.ffmpeg,source,99,
                            {'x':0,'y':155,'width':620,'height':90},color=color)

    def test_source_ink_failure_is_explicit_and_has_no_size_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'blank.mkv'
            self.run_media(['-f','lavfi','-i','color=black:s=160x90:r=30:d=0.1','-c:v','ffv1',str(source)])
            with self.assertRaisesRegex(overlay.OverlayError,'source_ink_not_found'):
                overlay.measure_source_ink_height(self.ffmpeg,source,0,{'x':10,'y':10,'width':100,'height':40})
            with self.assertRaisesRegex(overlay.OverlayError,'invalid_box'):
                overlay.measure_source_ink_height(self.ffmpeg,source,0,{'x':120,'y':10,'width':100,'height':40})


if __name__=='__main__':
    unittest.main()
