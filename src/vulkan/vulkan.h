#ifndef GZH_VULKAN_H
#define GZH_VULKAN_H

#define GLFW_INCLUD_VULKAN
#include<GLFW/glfw3.h>

class Render
{
protected:

public:
    Render()=default;
    
public:
    ~Render()
    {
        this->destroy();
    }

public:
    void init();
    void destroy();
};

#endif