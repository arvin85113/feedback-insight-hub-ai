"""System tray menu for the local node launcher."""


def build_icon_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(44, 118, 89, 255))
    draw.text((18, 22), "FI", fill=(255, 255, 255, 255))
    return image


def run_tray(*, open_console, open_logs, autostart_enabled, set_autostart, on_exit):
    import pystray

    def exit_app(icon, _item):
        on_exit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("開啟主控台", lambda _icon, _item: open_console(), default=True),
        pystray.MenuItem("查看日誌", lambda _icon, _item: open_logs()),
        pystray.MenuItem(
            "開機自動啟動",
            lambda _icon, _item: set_autostart(not autostart_enabled()),
            checked=lambda _item: autostart_enabled(),
        ),
        pystray.MenuItem("結束", exit_app),
    )
    pystray.Icon("FeedbackInsightHub", build_icon_image(), "FeedBack IQ 本機節點", menu).run()
