from django.core.management.commands.runserver import Command as RunserverCommand


class Command(RunserverCommand):
    """自定义 runserver 命令，将默认端口改为 10086。

    使用方式保持不变：
    - python manage.py runserver           # 默认 127.0.0.1:10086
    - python manage.py runserver 0.0.0.0:10086  # 仍可手动指定
    """

    default_port = "10086"
