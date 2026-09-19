import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).parents[1] / 'pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class PipelineTests(unittest.TestCase):
    def test_orientation_copies_and_rotation_direction(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            image = Image.new('RGB', (3, 2), 'black')
            image.putpixel((0, 0), (255, 0, 0))
            exif = Image.Exif()
            exif[274] = 6
            original = source / 'test.png'
            image.save(original, exif=exif)
            before = original.read_bytes()
            pipeline.orient_photos(source, root / 'corrected')
            with Image.open(root / 'corrected/test.png.png') as corrected:
                self.assertEqual(corrected.size, (2, 3))
                self.assertEqual(corrected.getpixel((1, 0)), (255, 0, 0))
                self.assertIsNone(corrected.getexif().get(274))
            self.assertEqual(original.read_bytes(), before)
            rotated = pipeline.rotate_image(image, -90)
            self.assertEqual(rotated.getpixel((1, 0)), (255, 0, 0))
            for angle in (0, 90, 180, 270):
                self.assertEqual(pipeline.rotate_image(pipeline.rotate_image(image, angle), -angle).tobytes(), image.tobytes())
            with self.assertRaises(ValueError):
                pipeline.rotate_image(image, 45)
            with self.assertRaises(FileExistsError):
                pipeline.orient_photos(source, root / 'corrected')

    def test_video_intake_and_failure_cleanup(self):
        import av
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'capture with spaces.mp4'
            with av.open(str(source), mode='w') as container:
                stream = container.add_stream('mpeg4', rate=10)
                stream.width, stream.height, stream.pix_fmt = 64, 64, 'yuv420p'
                for index in range(20):
                    pixels = np.full((64, 64, 3), index * 10, dtype=np.uint8)
                    frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
            output = root / 'output'
            pipeline.intake_video(source, output, 'Generated fixture', True, interval=0.5, max_frames=3)
            provenance = json.loads((output / 'provenance.json').read_text())
            media = provenance['sourceMedia']
            self.assertEqual(len(media), 4)
            self.assertTrue(media[0]['extraction']['frameLimitReached'])
            self.assertEqual([item['derivedFrom']['secondsFromFirstFrame'] for item in media[1:]], [0, 0.5, 1])
            for item in media[1:]:
                self.assertTrue((output / item['path']).is_file())
                self.assertEqual(item['derivedFrom']['sha256'], media[0]['sha256'])
            report = json.loads((output / 'confidence.json').read_text())
            self.assertEqual(len(report['imageQuality']), 3)
            with self.assertRaises(FileExistsError):
                pipeline.intake_video(source, output, 'fixture')
            uncapped = root / 'uncapped'
            pipeline.intake_video(source, uncapped, 'fixture', interval=1)
            self.assertEqual(len(list((uncapped / 'frames').glob('*.png'))), 2)
            for interval in (0, -1, float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    pipeline.intake_video(source, root / 'invalid', 'fixture', interval=interval)
            bad = root / 'bad.mp4'
            bad.write_bytes(b'invalid video')
            with self.assertRaisesRegex(ValueError, 'decode video'):
                pipeline.intake_video(bad, root / 'failed', 'fixture')
            self.assertFalse((root / 'failed').exists())
            self.assertFalse(list(root.glob('.video-intake-*')))

    def test_quality_checks(self):
        from PIL import Image, ImageFilter
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid = np.indices((128, 128)).sum(axis=0) // 8 % 2
            sharp = Image.fromarray((grid * 255).astype('uint8'))
            sharp.save(root / 'sharp.png')
            sharp.filter(ImageFilter.GaussianBlur(4)).save(root / 'blur.png')
            Image.new('RGB', (64, 64), 'black').save(root / 'dark.png')
            Image.new('RGB', (64, 64), 'white').save(root / 'bright.png')
            (root / 'bad.jpg').write_bytes(b'not an image')
            self.assertGreater(pipeline.image_quality(root / 'sharp.png')['laplacianVariance'],
                               pipeline.image_quality(root / 'blur.png')['laplacianVariance'])
            self.assertIn('possible-blur-or-low-texture', pipeline.image_quality(root / 'blur.png')['flags'])
            self.assertIn('possibly-underexposed', pipeline.image_quality(root / 'dark.png')['flags'])
            self.assertIn('possibly-overexposed', pipeline.image_quality(root / 'bright.png')['flags'])
            self.assertIn('unreadable-image', pipeline.image_quality(root / 'bad.jpg')['flags'])
            pipeline.intake(root, root / 'report', 'Generated test fixtures', check_quality=True)
            report = json.loads((root / 'report' / 'confidence.json').read_text())
            self.assertEqual(len(report['imageQuality']), 5)
            self.assertIn('surface-coverage', report['checksNotPerformed'])
            self.assertIsNone(report['observedCoveragePercent'])

    def test_intake_duplicates_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photos = root / 'photos'
            photos.mkdir()
            for name in ('a.jpg', 'b.jpg'):
                (photos / name).write_bytes(b'identical fixture')
            output = root / 'report'
            pipeline.intake(photos, output, 'Test fixture')
            report = json.loads((output / 'confidence.json').read_text())
            self.assertEqual(report['uniquePhotoCount'], 1)
            self.assertEqual(report['exactDuplicates'], [{'path': 'b.jpg', 'duplicateOf': 'a.jpg'}])
            self.assertIsNone(report['observedCoveragePercent'])
            with self.assertRaises(FileExistsError):
                pipeline.intake(photos, output, 'Test fixture')

    def test_reject_external_resources_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / 'model.glb'
            document = json.dumps({'meshes': [{}], 'images': [{'uri': 'texture.png'}]}).encode()
            document += b' ' * (-len(document) % 4)
            data = struct.pack('<4sII', b'glTF', 2, 20 + len(document))
            data += struct.pack('<II', len(document), 0x4E4F534A) + document
            model.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'external resources'):
                pipeline.validate_glb(model)
            model.write_bytes(data[:-1])
            with self.assertRaisesRegex(ValueError, 'file length'):
                pipeline.validate_glb(model)


if __name__ == '__main__':
    unittest.main()
