Before enumerating files by hand, build the inventory with `embedding_semantic_search`:

1. `action='index', mode='baseline', repo_paths=['repo/<app_name>']` to build (or reuse, if
   already built this run) a semantic index of the target app.
2. `action='get_inventory', repo_paths=['repo/<app_name>']` to get its technology stack,
   route list, and high-risk (auth/authz/crypto/injection) modules.

Use that inventory as the backbone of your file collection -- it tells you the frameworks,
routes, and risk-flagged files up front. Use `action='search'` with topic queries (e.g.
"authentication", "SQL query construction") to pull in specific relevant files the inventory's
high-risk list didn't already surface. Only fall back to manually browsing the filesystem for
files the embedding tool didn't cover.

Enumerate all files and identify file paths that contain information relevant to the task.

Be thorough, missing a file here will cause problem in future analysis, however, this stage is critical in creating a proper analysis scope and should do it's best not to include unecessary files

A full assessment is not needed in this phase. We are only concerned with collecting the relevant file paths
Output a list of all relevant file paths.


## Report Output
Use the following output format

**[GROUP_NAME]:**
   - [FILE_PATH]: [DESCRIPTION]
   - [FILE_PATH]: [DESCRIPTION]
   - ...

**[GROUP_NAME]:**
   - [FILE_PATH]: [DESCRIPTION]
   - [FILE_PATH]: [DESCRIPTION]
   - ...

...
