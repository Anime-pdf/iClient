import argparse
import csv
import os
import sys

import clang.cindex # pylint: disable=import-error

from clang.cindex import CursorKind, LinkageKind, StorageClass, TypeKind # pylint: disable=import-error

try:
	from tqdm import tqdm
except ImportError:
	def tqdm(it, *_args, **_kwargs):
		return it

def traverse_namespaced(root, filter_files=None, skip_namespaces=1, namespace=()):
	if root.location.file is not None and root.location.file.name not in filter_files:
		return
	yield namespace, root
	if root.displayname != "":
		if skip_namespaces > 0:
			skip_namespaces -= 1
		else:
			namespace += (root.spelling,)
	for node in root.get_children():
		yield from traverse_namespaced(node, filter_files, skip_namespaces, namespace)

INTERESTING_NODE_KINDS = {
	CursorKind.CLASS_DECL: "class",
	CursorKind.CLASS_TEMPLATE: "class",
	CursorKind.ENUM_DECL: "enum",
	CursorKind.ENUM_CONSTANT_DECL: "enum_constant",
	CursorKind.FIELD_DECL: "variable",
	CursorKind.PARM_DECL: "variable",
	CursorKind.STRUCT_DECL: "struct",
	CursorKind.UNION_DECL: "union",
	CursorKind.VAR_DECL: "variable",
	CursorKind.FUNCTION_DECL: "function",
}

def is_array_type(typ):
	return typ.kind in (TypeKind.CONSTANTARRAY, TypeKind.DEPENDENTSIZEDARRAY, TypeKind.INCOMPLETEARRAY)

def get_complex_type(typ):
	if typ.spelling in ("IOHANDLE", "LOCK"):
		return ""
	if typ.kind == TypeKind.AUTO:
		return get_complex_type(typ.get_canonical())
	if typ.kind == TypeKind.LVALUEREFERENCE:
		return get_complex_type(typ.get_pointee())
	if typ.kind == TypeKind.POINTER:
		return "p" + get_complex_type(typ.get_pointee())
	if is_array_type(type):
		return "a" + get_complex_type(typ.element_type)
	if typ.kind == TypeKind.FUNCTIONPROTO:
		return "fn"
	if typ.kind == TypeKind.TYPEDEF:
		return get_complex_type(typ.get_declaration().underlying_typedef_type)
	if typ.kind == TypeKind.ELABORATED:
		return get_complex_type(typ.get_named_type())
	if typ.kind in (TypeKind.UNEXPOSED, TypeKind.RECORD):
		if typ.get_declaration().spelling in "shared_ptr unique_ptr".split():
			return "p" + get_complex_type(typ.get_template_argument_type(0))
		if typ.get_declaration().spelling in "array sorted_array".split():
			return "a" + get_complex_type(typ.get_template_argument_type(0))
	return ""

def is_static_member_definition_hack(node):
	last_colons = False
	for t in node.get_tokens():
		t = t.spelling
		if t == "::":
			last_colons = True
		elif last_colons:
			if t.startswith("ms_"):
				return True
			last_colons = False
		if t == "=":
			return False
	return False

def is_const(typ):
	if typ.is_const_qualified():
		return True
	if is_array_type(type):
		return is_const(typ.element_type)
	return False

class ParseError(RuntimeError):
	pass

def process_source_file(out, file, extra_args, break_on):
	args = extra_args + ["-Isrc"]
	if file.endswith(".c"):
		header = f"{file[:-2]}.h"
	elif file.endswith(".cpp"):
		header = f"{file[:-4]}.h"
	else:
		raise ValueError(f"unrecognized source file: {file}")

	index = clang.cindex.Index.create()
	unit = index.parse(file, args=args)
	errors = list(unit.diagnostics)
	if errors:
		for error in errors:
			print(f"{file}: {error.format()}", file=sys.stderr)
		print(args, file=sys.stderr)
		raise ParseError(f"failed parsing {file}")

	filter_files = frozenset([file, header])

	for namespace, node in traverse_namespaced(unit.cursor, filter_files=filter_files):
		cur_file = None
		if node.location.file is not None:
			cur_file = node.location.file.name
		if cur_file is None or cur_file not in (file, header):
			continue
		if node.kind in INTERESTING_NODE_KINDS and node.spelling:
			typ = get_complex_type(node.type)
			qualifiers = ""
			if INTERESTING_NODE_KINDS[node.kind] in {"variable", "function"}:
				is_member = node.semantic_parent.kind in {CursorKind.CLASS_DECL, CursorKind.CLASS_TEMPLATE, CursorKind.STRUCT_DECL, CursorKind.UNION_DECL}
				is_static = node.storage_class == StorageClass.STATIC or is_static_member_definition_hack(node)
				if is_static:
					qualifiers = "s" + qualifiers
				if is_member:
					qualifiers = "m" + qualifiers
				if is_static and not is_member and is_const(node.type):
					qualifiers = "c" + qualifiers
				if node.linkage == LinkageKind.EXTERNAL and not is_member:
					qualifiers = "g" + qualifiers
			out.writerow({
				"file": cur_file,
				"line": node.location.line,
				"column": node.location.column,
				"kind": INTERESTING_NODE_KINDS[node.kind],
				"path": "::".join(namespace),
				"qualifiers": qualifiers,
				"type": typ,
				"name": node.spelling,
			})
			if node.spelling == break_on:
				breakpoint() # pylint: disable=forgotten-debug-statement

def main():
	p = argparse.ArgumentParser(description="Extracts identifier data from a Teeworlds source file and its header, outputting the data as CSV to stdout")
	p.add_argument("file", metavar="FILE", nargs="+", help="Source file to analyze")
	p.add_argument("--break-on", help="Break on a specific variable name, useful to debug issues with the script")
	args = p.parse_args()

	extra_args = []
	if "CXXFLAGS" in os.environ:
		extra_args = os.environ["CXXFLAGS"].split()

	out = csv.DictWriter(sys.stdout, "file line column kind path qualifiers type name".split())
	out.writeheader()
	files = args.file
	if len(files) > 1:
		files = tqdm(files, leave=False)
	error = False
	for file in files:
		try:
			process_source_file(out, file, extra_args, args.break_on)
		except ParseError:
			error = True
	return int(error)

if __name__ == "__main__":
	sys.exit(main())
