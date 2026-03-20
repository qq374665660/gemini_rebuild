import os

def get_directory_tree(path):
    """Generates a nested dictionary representing the directory tree."""
    tree = []
    if not os.path.isdir(path):
        return tree

    for entry in os.scandir(path):
        node = {
            'name': entry.name,
            'path': entry.path,
        }
        if entry.is_dir():
            node['type'] = 'directory'
            node['children'] = get_directory_tree(entry.path)
        else:
            node['type'] = 'file'
        tree.append(node)
    return tree
