#include <cstdio>

#include <FFT/FFT.h>
#include <GLFW/glfw3.h>
#include <assimp/scene.h>

#include "Support.h"

int main()
{
    puts("Hello Main");
    
    supportSay();

    fftSay();

    glfwInit();

    aiNode X;

    return 0;
}