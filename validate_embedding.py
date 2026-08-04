"""Local validation for the Embedding & Semantic Search Tool -- no LLM/Bedrock required.

Exercises embedding_tool's actions (index/baseline, get_inventory, search, and optionally
get_blast_radius) directly against a real checked-out app under repo/, so you can confirm
the tool works before running it through the full (LLM-driven) deepagent.py pipeline.

Usage:
  python validate_embedding.py
  python validate_embedding.py --app vtm --query "SQL query execution"
  python validate_embedding.py --app vtm --changed-file taskManager/views.py
"""

from argparse import ArgumentParser
from pathlib  import Path
import sys

ROOT = Path( __file__ ).parent.resolve()
sys.path.insert( 0, str( ROOT ) )

from tools.embedding_tool import EmbeddingSemanticSearchTool


def _section( title:str ) -> None:
  print( f"\n{'=' * 70}\n{title}\n{'=' * 70}" )


def main() -> None:
  cmdline = ArgumentParser( description='Locally validate embedding_tool against a checked-out app under repo/' )
  cmdline.add_argument( '--app'         , default='vtm', help='App name -- subfolder under repo/ (default: vtm)' )
  cmdline.add_argument( '--query'       , default='authentication and password handling', help='Semantic search query to test' )
  cmdline.add_argument( '--changed-file', default=None, help='File (relative to the app root) to test blast-radius on, e.g. taskManager/views.py' )
  cmdline.add_argument( '--context-dir' , default='output', help='Where to persist the index (default: output/)' )
  cmdline.add_argument( '--top-k'       , type=int, default=5, help='Number of search results to return (default: 5)' )
  args = cmdline.parse_args()

  repo_path = str( Path( ROOT, 'repo', args.app ) )
  if not Path( repo_path ).is_dir():
    print( f'[FAIL] No app found at {repo_path}' )
    sys.exit( 1 )

  tool = EmbeddingSemanticSearchTool( context_dir=args.context_dir )

  _section( f'1. Baseline index: repo/{args.app}' )
  print( tool._run( action='index', mode='baseline', repo_paths=[repo_path] ) )

  _section( f'2. Inventory: repo/{args.app}' )
  print( tool._run( action='get_inventory', repo_paths=[repo_path] ) )

  _section( f"3. Semantic search: '{args.query}'" )
  print( tool._run( action='search', query=args.query, top_k=args.top_k ) )

  if args.changed_file:
    _section( f'4. Blast radius: {args.changed_file}' )
    print( tool._run( action='get_blast_radius', changed_files=[args.changed_file] ) )
  else:
    _section( '4. Blast radius: skipped (pass --changed-file <path> to test it)' )

  _section( 'Done' )
  print( f"Persisted context: {Path( args.context_dir, args.app, 'context', 'embeddings' ).resolve()}" )


if __name__ == '__main__':
  main()
