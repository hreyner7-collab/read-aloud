"""Floating Read Aloud button for macOS (PyObjC).

A small round button that stays in front of every window, never takes focus
(so the app you're reading in keeps its selection), and can be dragged anywhere.
Right-click (or Ctrl-click) for voice, speed, open-at-login and quit.
"""
import objc
from AppKit import (
    NSAlert,
    NSApp,
    NSApplication,
    NSAppearance,
    NSAppearanceNameDarkAqua,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSColor,
    NSControlSizeSmall,
    NSEvent,
    NSEventModifierFlagControl,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSPanel,
    NSPointInRect,
    NSProgressIndicator,
    NSProgressIndicatorStyleSpinning,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import selection_mac, settings as st
from .controller import Controller

SIZE = 52      # window
CIRCLE = 44    # button

LOOK = {  # state: (symbol, background colour)
    "idle": ("speaker.wave.2.fill", lambda: NSColor.colorWithCalibratedWhite_alpha_(0.13, 0.92)),
    "grabbing": (None, lambda: NSColor.colorWithCalibratedWhite_alpha_(0.13, 0.92)),
    "loading": (None, lambda: NSColor.colorWithCalibratedWhite_alpha_(0.13, 0.92)),
    "speaking": ("stop.fill", lambda: NSColor.systemBlueColor()),
    "empty": ("character.cursor.ibeam", lambda: NSColor.systemOrangeColor()),
    "permission": ("lock.fill", lambda: NSColor.systemOrangeColor()),
    "error": ("exclamationmark.triangle.fill", lambda: NSColor.systemRedColor()),
}
TIPS = {
    "idle": "Select text anywhere, then click to read it aloud",
    "speaking": "Click to stop",
    "empty": "Select some text first",
    "permission": "Allow Read Aloud in System Settings > Privacy & Security > Accessibility",
    "error": "Couldn't read aloud - check your internet connection",
}


def symbol(name):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is not None:
        img.setTemplate_(True)
    return img


class ButtonView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(ButtonView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.state = "idle"
        self.controller = None
        self.app_ = None
        self.menu_ = None
        self._down = None
        inset = (SIZE - CIRCLE) / 2
        icon = 20
        self.icon = NSImageView.alloc().initWithFrame_(
            NSMakeRect((SIZE - icon) / 2, (SIZE - icon) / 2, icon, icon))
        self.icon.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        self.icon.setContentTintColor_(NSColor.whiteColor())
        self.addSubview_(self.icon)
        sp = 18
        self.spinner = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect((SIZE - sp) / 2, (SIZE - sp) / 2, sp, sp))
        self.spinner.setStyle_(NSProgressIndicatorStyleSpinning)
        self.spinner.setControlSize_(NSControlSizeSmall)
        self.spinner.setDisplayedWhenStopped_(False)
        self.spinner.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua))
        self.addSubview_(self.spinner)
        self.inset = inset
        self.show_("idle")
        return self

    # every click lands on the button itself, not the icon inside it
    def hitTest_(self, point):
        return self if NSPointInRect(point, self.frame()) else None

    def acceptsFirstMouse_(self, event):
        return True

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        from AppKit import NSBezierPath
        _, colour = LOOK.get(self.state, LOOK["idle"])
        colour().setFill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(self.inset, self.inset, CIRCLE, CIRCLE)).fill()
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).setStroke()
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(self.inset + 0.5, self.inset + 0.5, CIRCLE - 1, CIRCLE - 1))
        ring.setLineWidth_(1.0)
        ring.stroke()

    def show_(self, state):
        self.state = state
        name, _ = LOOK.get(state, LOOK["idle"])
        if name:
            self.icon.setImage_(symbol(name))
            self.icon.setHidden_(False)
            self.spinner.stopAnimation_(None)
        else:
            self.icon.setHidden_(True)
            self.spinner.startAnimation_(None)
        self.setToolTip_(TIPS.get(state, ""))
        self.setNeedsDisplay_(True)

    # ---- mouse: click = read/stop, drag = move, right-click = menu
    def mouseDown_(self, event):
        if event.modifierFlags() & NSEventModifierFlagControl:
            self.rightMouseDown_(event)
            return
        self._down = NSEvent.mouseLocation()
        self._origin = self.window().frame().origin
        self._dragged = False

    def mouseDragged_(self, event):
        if self._down is None:
            return
        p = NSEvent.mouseLocation()
        dx, dy = p.x - self._down.x, p.y - self._down.y
        if abs(dx) + abs(dy) > 3:
            self._dragged = True
        if self._dragged:
            self.window().setFrameOrigin_((self._origin.x + dx, self._origin.y + dy))

    def mouseUp_(self, event):
        if self._down is None:
            return
        self._down = None
        if self._dragged:
            o = self.window().frame().origin
            self.controller.moved(o.x, o.y)
        else:
            self.controller.click()

    def rightMouseDown_(self, event):
        self.app_.refresh_menu()  # so the offline-voice status is current
        NSMenu.popUpContextMenu_withEvent_forView_(self.menu_, event, self)


class MenuHandler(NSObject):
    def initWithApp_(self, app):
        self = objc.super(MenuHandler, self).init()
        self.app = app
        return self

    def pickVoice_(self, sender):
        self.app.controller.set_voice(sender.representedObject())
        self.app.refresh_menu()

    def pickSpeed_(self, sender):
        self.app.controller.set_rate(sender.representedObject())
        self.app.refresh_menu()

    def toggleOffline_(self, sender):
        self.app.controller.set_offline_only(not self.app.settings.get("offline_only"))
        self.app.refresh_menu()

    def toggleLogin_(self, sender):
        st.set_login_item(not st.login_item_enabled())
        self.app.refresh_menu()

    def showHelp_(self, sender):
        self.app.welcome(force=True)

    def quit_(self, sender):
        NSApp.terminate_(None)


class MacApp:
    def __init__(self, selftest=None):
        self.selftest = selftest
        self.nsapp = NSApplication.sharedApplication()
        # Show in the Dock so it can be quit like any app (right-click > Quit, or Cmd+Q)
        self.nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self._build_main_menu()
        self.settings = st.load()
        self.controller = Controller(AppHelper.callAfter, selection_mac, self.settings)
        self._build_window()
        self.controller.view = self
        self._build_menu()

    def _build_main_menu(self):
        bar = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        bar.addItem_(app_item)
        app_menu = NSMenu.alloc().initWithTitle_("Read Aloud")
        app_menu.addItemWithTitle_action_keyEquivalent_("Quit Read Aloud", "terminate:", "q")
        app_item.setSubmenu_(app_menu)
        self.nsapp.setMainMenu_(bar)

    # ---- used by Controller
    def show(self, state):
        self.view.show_(state)

    def later(self, seconds, fn):
        AppHelper.callLater(seconds, fn)

    # ---- window
    def _position(self):
        x, y = self.settings.get("x"), self.settings.get("y")
        if x is not None and y is not None:
            for s in NSScreen.screens():
                if NSPointInRect((x + SIZE / 2, y + SIZE / 2), s.frame()):
                    return x, y
        vf = NSScreen.mainScreen().visibleFrame()
        return vf.origin.x + vf.size.width - SIZE - 24, vf.origin.y + vf.size.height / 2

    def _build_window(self):
        x, y = self._position()
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, SIZE, SIZE),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        p.setLevel_(NSStatusWindowLevel)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(True)
        p.setHidesOnDeactivate_(False)
        p.setBecomesKeyOnlyIfNeeded_(True)
        p.setFloatingPanel_(True)
        p.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary)
        v = ButtonView.alloc().initWithFrame_(NSMakeRect(0, 0, SIZE, SIZE))
        v.controller = self.controller
        p.setContentView_(v)
        p.orderFrontRegardless()
        self.panel, self.view = p, v

    # ---- right-click menu
    def _build_menu(self):
        self.handler = MenuHandler.alloc().initWithApp_(self)
        self.menu = NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)
        self.view.menu_ = self.menu
        self.view.app_ = self
        self.refresh_menu()

    def refresh_menu(self):
        m = self.menu
        m.removeAllItems()
        h = self.handler

        def item(title, action, obj=None, on=False, parent=m):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            it.setTarget_(h)
            if obj is not None:
                it.setRepresentedObject_(obj)
            it.setState_(1 if on else 0)
            parent.addItem_(it)
            return it

        def submenu(title):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            sub = NSMenu.alloc().initWithTitle_(title)
            it.setSubmenu_(sub)
            m.addItem_(it)
            return sub

        voices = submenu("Voice")
        for vid, label in st.VOICES:
            item(label, "pickVoice:", vid, vid == self.settings["voice"], voices)
        voices.addItem_(NSMenuItem.separatorItem())
        item("Offline voice only (works without internet)", "toggleOffline:",
             on=bool(self.settings.get("offline_only")), parent=voices)
        status = item(self.controller.offline_status(), None, parent=voices)
        status.setEnabled_(False)
        speeds = submenu("Speed")
        for rate, label in st.SPEEDS:
            item(label, "pickSpeed:", rate, rate == self.settings["rate"], speeds)
        m.addItem_(NSMenuItem.separatorItem())
        if st.login_item_supported():
            item("Open at Login", "toggleLogin:", on=st.login_item_enabled())
        item("How to use", "showHelp:")
        m.addItem_(NSMenuItem.separatorItem())
        item("Quit Read Aloud", "quit:")

    # ---- first run
    def welcome(self, force=False):
        if self.settings.get("welcomed") and not force:
            return
        self.settings["welcomed"] = True
        st.save(self.settings)
        NSApp.activateIgnoringOtherApps_(True)
        a = NSAlert.alloc().init()
        a.setMessageText_("Read Aloud")
        a.setInformativeText_(
            "Select text in any app, then click the round button to hear it read aloud. "
            "Click again to stop.\n\n"
            "Drag the button wherever you like. Right-click it for voices and speed.\n\n"
            "The first time, macOS will ask you to allow Read Aloud under Privacy & "
            "Security > Accessibility. It needs that to see the text you've selected.")
        a.addButtonWithTitle_("OK")
        a.runModal()

    def run(self):
        if self.selftest:
            from .selftest import run_selftest
            run_selftest(self.controller, self.selftest,
                         lambda: AppHelper.callAfter(NSApp.terminate_, None))
        else:
            AppHelper.callLater(0.5, self._first_run)
        AppHelper.runEventLoop(installInterrupt=False)

    def _first_run(self):
        self.welcome()
        if not selection_mac.has_permission():
            selection_mac.ask_permission()
