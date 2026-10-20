#include <SDL.h>
#include <SDL_syswm.h>
#include <X11/Xlib.h>
#include <X11/extensions/XTest.h>
#include <X11/keysym.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>

int main(int argc, char** argv)
{
    bool level3 = false;
    bool hold = false;
    bool passthrough = false;
    bool refocus = false;
    for (int i = 1; i < argc; i++) {
        if (std::strcmp(argv[i], "--level3") == 0) {
            level3 = true;
        } else if (std::strcmp(argv[i], "--hold") == 0) {
            hold = true;
        } else if (std::strcmp(argv[i], "--passthrough") == 0) {
            passthrough = true;
        } else if (std::strcmp(argv[i], "--refocus") == 0) {
            refocus = true;
        } else {
            std::fprintf(stderr, "Unknown argument: %s\n", argv[i]);
            return 1;
        }
    }

    XInitThreads();
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_TIMER) != 0) {
        std::fprintf(stderr, "SDL init failed: %s\n", SDL_GetError());
        return 1;
    }
    auto* window = SDL_CreateWindow("X11 Alt capture regression", 0, 0, 640, 480, 0);
    if (!window) {
        std::fprintf(stderr, "SDL window failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_SysWMinfo info;
    SDL_VERSION(&info.version);
    if (!SDL_GetWindowWMInfo(window, &info) || info.subsystem != SDL_SYSWM_X11) {
        std::fprintf(stderr, "Run this test on an isolated X11 display using xvfb-run\n");
        return 1;
    }
    auto capture = [&](bool active) {
        if (SDL_SetRelativeMouseMode(active ? SDL_TRUE : SDL_FALSE) != 0) {
            std::fprintf(stderr, "Mouse capture failed: %s\n", SDL_GetError());
            std::exit(1);
        }
        SDL_SetWindowKeyboardGrab(window, active ? SDL_TRUE : SDL_FALSE);
        if (SDL_GetWindowKeyboardGrab(window) != (active ? SDL_TRUE : SDL_FALSE)) {
            std::fprintf(stderr, "Keyboard capture failed\n");
            std::exit(1);
        }
    };
    XSetInputFocus(info.info.x11.display, info.info.x11.window, RevertToParent, CurrentTime);
    XSync(info.info.x11.display, False);
    capture(true);

    constexpr unsigned presses = 6;
    const unsigned holdMs = hold ? 750 : 40;
    std::thread injector([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        auto* display = XOpenDisplay(nullptr);
        if (!display) {
            std::fprintf(stderr, "Cannot connect XTest injector\n");
            std::exit(1);
        }
        const auto key = XKeysymToKeycode(display, level3 ? XK_ISO_Level3_Shift : XK_Alt_R);
        if (!key) {
            std::fprintf(stderr, "Requested physical key has no X11 mapping\n");
            std::exit(1);
        }
        for (unsigned i = 0; i < presses; i++) {
            if (!XTestFakeKeyEvent(display, key, True, CurrentTime)) {
                std::fprintf(stderr, "XTest key press failed\n");
                std::exit(1);
            }
            XSync(display, False);
            std::this_thread::sleep_for(std::chrono::milliseconds(holdMs));
            if (!XTestFakeKeyEvent(display, key, False, CurrentTime)) {
                std::fprintf(stderr, "XTest key release failed\n");
                std::exit(1);
            }
            XSync(display, False);
            if (refocus) {
                XSetInputFocus(display, PointerRoot, RevertToParent, CurrentTime);
                XSync(display, False);
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
                XSetInputFocus(display, info.info.x11.window, RevertToParent, CurrentTime);
                XSync(display, False);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        XCloseDisplay(display);
    });

    unsigned down = 0, up = 0, repeated = 0, transitions = 0;
    const auto deadline = SDL_GetTicks64() + 200 + presses * (holdMs + 120) + 1000;
    while (SDL_GetTicks64() < deadline) {
        SDL_Event event;
        if (!SDL_WaitEventTimeout(&event, 10) ||
                (event.type != SDL_KEYDOWN && event.type != SDL_KEYUP) ||
                event.key.keysym.scancode != SDL_SCANCODE_RALT) {
            continue;
        }
        if (event.type == SDL_KEYUP) {
            up++;
        } else if (event.key.repeat) {
            repeated++;
        } else {
            down++;
            if (!passthrough) {
                capture(SDL_GetRelativeMouseMode() != SDL_TRUE);
                transitions++;
            }
        }
    }
    injector.join();
    const bool passed = down == presses && up == presses &&
                        transitions == (passthrough ? 0 : presses) &&
                        SDL_GetRelativeMouseMode() == SDL_TRUE;
    std::printf("%s: down=%u up=%u repeat=%u transitions=%u (physical presses=%u)\n",
                passed ? "PASS" : "FAIL", down, up, repeated, transitions, presses);
    capture(false);
    SDL_DestroyWindow(window);
    SDL_Quit();
    return passed ? 0 : 1;
}
