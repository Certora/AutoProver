"""Single home for the editing-workflow tool names, so registration sites and
the messages that tell an agent to call a sibling tool cannot drift apart.
The VFS read/write tool names (``get_file``, ``edit_file``, ...) are minted by
graphcore and are not repeated here.
"""

# The author's edit-management surface.
CODE_EDITOR = "code_editor"
COMMIT_EDIT = "commit_edit"
CONFIG_EDIT = "config_edit"
EDIT_HISTORY_LOG = "edit_history_log"
REVERT_TO_EDIT = "revert_to_edit"

# The editor sub-agent's completion surface.
REQUEST_REVIEW = "request_review"
SUBMIT_EDIT = "submit_edit"
GIVE_UP = "give_up"

# The editor sub-agent's helpers.
ERC7201_SLOT = "erc7201_slot"
KECCAK_STRING = "keccak256_string"
