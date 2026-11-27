import unittest

from video_localizer.frame_program import Program, ProgramError, validate


class FrameProgramTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.original = np.full((20,30,3), [30,60,100], dtype=np.uint8)
        self.original[4:10,8:18] = 255
        self.original[0:2,0:2] = [255,0,0]

    def test_program_repairs_background_and_protects_foreground_without_changing_input(self):
        before = self.original.copy()
        result = Program("background = solid([30,60,100])\nforeground_mask = color_mask(original,[255,0,0],20)\ntext_alpha = 0.75").run(self.original)
        self.assertTrue((result["background"] == [30,60,100]).all())
        self.assertTrue(result["foreground_mask"][0,0])
        self.assertFalse(result["foreground_mask"][6,12])
        self.assertEqual(result["text_alpha"], .75)
        self.np.testing.assert_array_equal(before,self.original)

    def test_neighbor_reference_can_be_combined_using_program_mask(self):
        ref = self.np.full_like(self.original,[30,60,100])
        code = "letters = light_mask(original,200,20)\nbackground = where(letters[...,None],reference(0),original)"
        result = Program(code).run(self.original,references=[ref])
        self.np.testing.assert_array_equal(result["background"][6,12],ref[6,12])
        self.np.testing.assert_array_equal(result["background"][0,0],self.original[0,0])

    def test_program_can_use_bounded_conditional_animation(self):
        code = "background = inpaint(original,light_mask(original),3)\nif progress < 0.5:\n    text_alpha = progress * 2\nelse:\n    text_alpha = 1"
        self.assertEqual(Program(code).run(self.original,progress=.2)["text_alpha"],.4)

    def test_system_access_and_unbounded_control_flow_are_rejected(self):
        examples = ["import os\nbackground=original", "background=original.__class__", "background=__import__('os')",
                    "background=open('secret')", "while True:\n    background=original", "for x in original:\n    background=x",
                    "background=[x for x in original]", "background=(lambda:original)()", "original=solid([0,0,0])",
                    "original[0]=0\nbackground=original", "background=original ** 1000000"]
        for code in examples:
            with self.subTest(code=code), self.assertRaises(ProgramError):
                validate(code)

    def test_integer_growth_is_stopped_before_large_computation(self):
        with self.assertRaises(ProgramError):
            Program("value=1000000\nvalue=value*value\nbackground=original").run(self.original)

    def test_broadcast_expansion_is_rejected_before_allocation(self):
        for expression in ("original[:,None,:,:]+original[None,:,:,:]", "where(True,original[:,None,:,:],original[None,:,:,:])"):
            with self.subTest(expression=expression), self.assertRaises(ProgramError):
                Program("background="+expression).run(self.original)

    def test_advanced_indexing_is_not_allowed(self):
        with self.assertRaises(ProgramError):
            Program("background=original[original]").run(self.original)

    def test_wrong_shape_and_opacity_fail(self):
        for code in ("background=original[0]", "background=original\ntext_alpha=2", "background=original\nforeground_mask=original"):
            with self.subTest(code=code), self.assertRaises(ProgramError):
                Program(code).run(self.original)

    def test_a_branch_must_produce_background(self):
        with self.assertRaises(ProgramError):
            Program("if progress > 0.9:\n    background=original").run(self.original,progress=0)


if __name__ == "__main__":
    unittest.main()
