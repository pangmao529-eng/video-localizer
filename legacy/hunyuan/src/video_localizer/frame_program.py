"""Small interpreted Python expression language for model-authored region repair.

This is a bounded syntax interpreter, not an OS sandbox. There is no Python exec,
imports, object attribute access, file I/O, loops, reflection, network or shell.
The normal renderer always draws the plan's target text after this program.
"""
from __future__ import annotations

import ast
from collections import OrderedDict
import math
import operator


class ProgramError(ValueError):
    pass


HELPERS = {"solid", "where", "clip", "minimum", "maximum", "absolute", "mean", "blend", "blur",
           "inpaint", "shift", "dilate", "erode", "light_mask", "dark_mask", "color_mask", "reference"}
INPUTS = {"original", "previous", "mask", "mask_valid", "frame_index", "progress", "width", "height"}
OUTPUTS = {"background", "foreground_mask", "text_alpha"}
ALLOWED = (ast.Module, ast.Assign, ast.If, ast.IfExp, ast.Name, ast.Load, ast.Store, ast.Constant,
           ast.List, ast.Tuple, ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp,
           ast.Call, ast.keyword, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
           ast.BitAnd, ast.BitOr, ast.BitXor, ast.UAdd, ast.USub, ast.Invert, ast.Not,
           ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.And, ast.Or)


def validate(code):
    if not isinstance(code, str) or not code.strip() or len(code) > 8000:
        raise ProgramError("Region program must contain 1–8000 characters")
    try:
        tree = ast.parse(code, mode="exec")
    except (SyntaxError, ValueError) as error:
        raise ProgramError("Region program syntax is invalid") from error
    nodes = list(ast.walk(tree))
    if len(nodes) > 300:
        raise ProgramError("Region program exceeds 300 syntax nodes")
    assignments = 0
    def depth(node, level=0):
        if level > 20:
            raise ProgramError("Region program nesting is too deep")
        for child in ast.iter_child_nodes(node):
            depth(child, level+1)
    depth(tree)
    for node in nodes:
        if not isinstance(node, ALLOWED):
            raise ProgramError(f"Unsupported region program syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and (node.id.startswith("_") or len(node.id) > 48):
            raise ProgramError("Private or oversized identifiers are not available")
        if isinstance(node, ast.Constant):
            value = node.value
            if value is not None and value is not Ellipsis and (type(value) not in (int, float, bool)
                    or not math.isfinite(value) or abs(value) > 1_000_000):
                raise ProgramError("Only bounded numeric, boolean, None and slice constants are allowed")
        if isinstance(node, ast.Assign):
            assignments += 1
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise ProgramError("Assign only to one variable; pixels cannot be mutated through subscripts")
            if node.targets[0].id in INPUTS | HELPERS:
                raise ProgramError("Program inputs and helpers are read-only")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in HELPERS:
                raise ProgramError("Call only documented image helpers")
            if any(key.arg is None or key.arg.startswith("_") for key in node.keywords):
                raise ProgramError("Expanded/private keyword arguments are not allowed")
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) > 16:
            raise ProgramError("Literal arrays are limited to 16 elements")
    if assignments > 32:
        raise ProgramError("Region program is limited to 32 assignments")
    if not any(isinstance(n, ast.Assign) and n.targets[0].id == "background" for n in nodes):
        raise ProgramError("Program must assign background")
    return tree


class Program:
    def __init__(self, code):
        self.tree = validate(code)

    def run(self, original, *, previous=None, mask=None, references=(), frame_index=0, progress=0):
        import numpy as np
        import cv2
        h, w = original.shape[:2]
        if original.shape != (h, w, 3) or original.dtype != np.uint8 or h*w > 2_100_000:
            raise ProgramError("Invalid region pixels")
        env = {"original": original, "previous": previous if previous is not None else original,
               "mask": np.zeros((h,w), dtype=bool) if mask is None else mask != 0,
               "mask_valid": mask is not None, "frame_index": frame_index, "progress": float(progress),
               "width": w, "height": h}
        limit = h*w*3

        def guard(value):
            if isinstance(value, np.ndarray):
                if value.dtype.kind not in "buif" or value.size > limit or not np.isfinite(value).all():
                    raise ProgramError("Program produced invalid or oversized pixels")
            elif isinstance(value, np.generic):
                guard(value.item())
            elif type(value) in (int, float, bool):
                if not math.isfinite(value) or abs(value) > 1_000_000_000:
                    raise ProgramError("Program scalar exceeded its bound")
            elif isinstance(value, (list, tuple)):
                if len(value) > 16:
                    raise ProgramError("Program literal exceeded its bound")
                for item in value:
                    guard(item)
            elif value is not None and value is not Ellipsis and not isinstance(value, slice):
                raise ProgramError("Unsupported program value")
            return value

        def pixels(value):
            value = np.asarray(value)
            guard(value)
            if value.shape != (h,w,3):
                raise ProgramError("Image helpers require the current region shape")
            return np.clip(value, 0, 255).astype(np.uint8)

        def stencil(value):
            value = np.asarray(value)
            if value.shape != (h,w) or value.dtype.kind not in "buif":
                raise ProgramError("Masks must match the current region")
            return value != 0

        def number(value, low, high, integer=False):
            if isinstance(value, np.generic):
                value = value.item()
            if type(value) not in (int, float) or not low <= value <= high or (integer and int(value) != value):
                raise ProgramError("Image helper numeric argument is out of range")
            return int(value) if integer else value

        def solid(color):
            color = np.asarray(color)
            if color.shape != (3,) or color.dtype.kind not in "uif" or np.any(color < 0) or np.any(color > 255):
                raise ProgramError("solid expects three RGB values in 0..255")
            return np.broadcast_to(color, (h,w,3)).astype(np.uint8)

        def blend(first, second, amount):
            first, second = pixels(first), pixels(second)
            if isinstance(amount, np.ndarray):
                amount = stencil(amount).astype(float)[...,None]
            else:
                amount = number(amount, 0, 1)
            return first.astype(float)*(1-amount)+second.astype(float)*amount

        def blur(image, radius=3):
            radius = number(radius, 0, 25, True)
            return cv2.GaussianBlur(pixels(image), (2*radius+1, 2*radius+1), 0) if radius else pixels(image)

        def inpaint(image, mask, radius=3):
            return cv2.inpaint(pixels(image), stencil(mask).astype(np.uint8)*255, number(radius, 1, 9), cv2.INPAINT_TELEA)

        def shift(image, dx=0, dy=0):
            image = pixels(image)
            dx, dy = number(dx, -w, w, True), number(dy, -h, h, True)
            result = image.copy()
            x0,x1,y0,y1 = max(0,dx),min(w,w+dx),max(0,dy),min(h,h+dy)
            if x1>x0 and y1>y0:
                result[y0:y1,x0:x1] = image[y0-dy:y1-dy,x0-dx:x1-dx]
            return result

        def morph(value, radius=1, erode=False):
            radius = number(radius, 0, 6, True)
            method = cv2.erode if erode else cv2.dilate
            return method(stencil(value).astype(np.uint8), np.ones((radius*2+1, radius*2+1), np.uint8)) != 0

        def light_mask(image, threshold=155, spread=100):
            rgb = pixels(image).astype(float)
            return (rgb.min(axis=2) > number(threshold,0,255)) & (rgb.max(axis=2)-rgb.min(axis=2) < number(spread,0,255))

        def dark_mask(image, threshold=90):
            return pixels(image).max(axis=2) < number(threshold,0,255)

        def color_mask(image, color, tolerance=40):
            return np.abs(pixels(image).astype(float)-solid(color)).max(axis=2) <= number(tolerance,0,255)

        def reference(index):
            index = number(index, 0, len(references)-1, True)
            return pixels(references[index])

        def mean(value, axis=None):
            array = np.asarray(value)
            guard(array)
            if axis is not None:
                axes = axis if isinstance(axis, (tuple,list)) else (axis,)
                for a in axes:
                    number(a, -array.ndim, array.ndim-1, True)
            return np.mean(array, axis=tuple(axis) if isinstance(axis,list) else axis)

        def broadcast(*values):
            shapes = [np.asarray(value).shape for value in values]
            try:
                size = math.prod(np.broadcast_shapes(*shapes))
            except ValueError:
                raise ProgramError("Incompatible image expression shapes") from None
            if size > limit:
                raise ProgramError("Image expression broadcasting exceeds the region limit")

        def elementwise(function):
            def bounded(*values, **keywords):
                if keywords:
                    raise ProgramError("Elementwise helpers take positional arguments")
                broadcast(*values)
                return function(*values)
            return bounded

        def where(condition, yes, no):
            broadcast(condition, yes, no)
            return np.where(condition, yes, no)

        helpers = {"solid": solid, "where": where, "clip": elementwise(np.clip), "minimum": elementwise(np.minimum),
                   "maximum": elementwise(np.maximum), "absolute": elementwise(np.abs), "mean": mean, "blend": blend, "blur": blur,
                   "inpaint": inpaint, "shift": shift, "dilate": morph, "erode": lambda mask,radius=1: morph(mask,radius,True),
                   "light_mask": light_mask, "dark_mask": dark_mask, "color_mask": color_mask, "reference": reference}
        binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
                  ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor}
        comparison = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
                      ast.Gt: operator.gt, ast.GtE: operator.ge}
        def truth(value):
            if isinstance(value, np.ndarray):
                raise ProgramError("Use where for array conditions")
            return bool(value)

        def evaluate(node):
            if isinstance(node, ast.Constant):
                result = node.value
            elif isinstance(node, ast.Name):
                if node.id not in env:
                    raise ProgramError(f"Unknown region variable: {node.id}")
                result = env[node.id]
            elif isinstance(node, (ast.List, ast.Tuple)):
                rows = [evaluate(x) for x in node.elts]
                result = tuple(rows) if isinstance(node, ast.Tuple) else rows
            elif isinstance(node, ast.Slice):
                result = slice(*(evaluate(x) if x is not None else None for x in (node.lower,node.upper,node.step)))
            elif isinstance(node, ast.Subscript):
                index = evaluate(node.slice)
                indices = index if isinstance(index,tuple) else (index,)
                if any(not (type(i) is int or i is None or i is Ellipsis or isinstance(i,slice)) for i in indices):
                    raise ProgramError("Only scalar and slice indexing is allowed")
                result = evaluate(node.value)[index]
            elif isinstance(node, ast.BinOp):
                left,right = evaluate(node.left),evaluate(node.right)
                if isinstance(left,(list,tuple)):
                    left=np.asarray(left)
                if isinstance(right,(list,tuple)):
                    right=np.asarray(right)
                broadcast(left,right)
                if type(node.op) not in (ast.BitAnd,ast.BitOr,ast.BitXor):
                    left = left.astype(float) if isinstance(left,np.ndarray) else left
                    right = right.astype(float) if isinstance(right,np.ndarray) else right
                result = binary[type(node.op)](left,right)
            elif isinstance(node, ast.UnaryOp):
                value = evaluate(node.operand)
                result = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Invert: operator.invert,
                          ast.Not: lambda x: not truth(x)}[type(node.op)](value)
            elif isinstance(node, ast.Compare):
                if len(node.ops) != 1:
                    raise ProgramError("Use separate comparisons combined with & or and")
                result = comparison[type(node.ops[0])](evaluate(node.left),evaluate(node.comparators[0]))
            elif isinstance(node, ast.BoolOp):
                result = truth(evaluate(node.values[0]))
                for item in node.values[1:]:
                    if isinstance(node.op,ast.And) and not result or isinstance(node.op,ast.Or) and result:
                        break
                    result = truth(evaluate(item))
            elif isinstance(node, ast.IfExp):
                result = evaluate(node.body if truth(evaluate(node.test)) else node.orelse)
            elif isinstance(node, ast.Call):
                result = helpers[node.func.id](*[evaluate(x) for x in node.args], **{x.arg:evaluate(x.value) for x in node.keywords})
            else:
                raise ProgramError("Unsupported expression")
            return guard(result)

        def execute(statements):
            for statement in statements:
                if isinstance(statement, ast.Assign):
                    env[statement.targets[0].id] = evaluate(statement.value)
                    if sum(v.nbytes for v in env.values() if isinstance(v,np.ndarray)) > 64*1024**2:
                        raise ProgramError("Program region memory limit exceeded")
                elif isinstance(statement,ast.If):
                    execute(statement.body if truth(evaluate(statement.test)) else statement.orelse)
                else:
                    raise ProgramError("Unsupported statement")
        try:
            execute(self.tree.body)
            if "background" not in env:
                raise ProgramError("Executed branch did not assign background")
            background = pixels(env["background"])
            foreground = stencil(env.get("foreground_mask", np.zeros((h,w),bool)))
            alpha = env.get("text_alpha", 1.0)
            if isinstance(alpha,np.ndarray):
                if alpha.shape != (h,w) or np.any(alpha<0) or np.any(alpha>1):
                    raise ProgramError("text_alpha must match region height/width and stay in 0..1")
            else:
                alpha = number(alpha,0,1)
            return {"background": background, "foreground_mask": foreground, "text_alpha": alpha}
        except ProgramError:
            raise
        except Exception as error:
            raise ProgramError(f"Region image operation failed: {type(error).__name__}") from None


class RegionPrograms:
    """Renderer hook; only receives already validated segment areas."""
    def __init__(self, programs, manifest, reference_pixels):
        self.programs = {sid: (Program(row["code"]),row) for sid,row in programs.items()}
        self.manifest = manifest
        self.references = reference_pixels
        self.previous = OrderedDict()

    def __call__(self, segment, index, original, box):
        if segment["id"] not in self.programs:
            return None
        from . import _engine as engine
        x,y,w,h = box
        patch = original[y:y+h,x:x+w]
        program, record = self.programs[segment["id"]]
        try:
            mask = engine.glyph_mask(patch,segment["mask"])
        except engine.BridgeError:
            mask = None
        previous = self.previous.get(segment["id"])
        if previous is not None and previous.shape != patch.shape:
            previous = None
        refs = [self.references[i][y:y+h,x:x+w] for i in record.get("reference_frames", [])]
        instant = self.manifest["frames"][index]["ptsMs"]
        progress = (instant-segment["startMs"])/max(1,segment["endMs"]-segment["startMs"])
        result = program.run(patch, previous=previous, mask=mask, references=refs, frame_index=index, progress=progress)
        self.previous[segment["id"]] = result["background"]
        self.previous.move_to_end(segment["id"])
        while sum(v.nbytes for v in self.previous.values()) > 16*1024**2 and len(self.previous)>1:
            self.previous.popitem(last=False)
        return result
