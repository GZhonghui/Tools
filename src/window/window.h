#ifndef WINDOW_H
#define WINDOW_H

class Window
{
protected:
    int window_width;
    int window_height;

protected:
    void (*key_down)(char);
    void (*key_release)(char);

    void (*mouse_down)(bool);
    void (*mouse_release)(bool);

public:
    Window():
    window_width(768),
    window_height(1024),

    key_down(nullptr),
    key_release(nullptr),

    mouse_down(nullptr),
    mouse_release(nullptr)
    {

    }

public:
    ~Window()
    {
        this->destroy();
    }

public:
    void set_key_event();

public:
    void init();
    void show();
    void destroy();
};

#endif