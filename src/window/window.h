#ifndef WINDOW_H
#define WINDOW_H

class Window
{
protected:
    int window_width;
    int window_height;

public:
    Window():
    window_width(768),
    window_height(1024)
    {

    }

public:
    ~Window()
    {
        this->destroy();
    }

public:
    void init();
    void show();
    void destroy();
};

#endif