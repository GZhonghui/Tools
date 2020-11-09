#ifndef CONTROL_H
#define CONTROL_H

class Control
{
public:
    Control()=default;

public:
    ~Control()=default;

public:
    static void key_down(char key);
    static void key_release(char key);

public:
    static void mouse_down(bool left);
    static void mouse_release(bool left);
};

#endif