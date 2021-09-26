all: Project

CC = g++

Target = SimpleWindow

InclDir  = -ISupport/Include
InclDir += -ISupport/Include/ImGUI
InclDir += -ISupport/Include/ImGUI/backends

LinkDir  = -LSupport/Lib

CompFlag = $(InclDir) -Wall -O2 -c --std=c++17
LinkFlag = $(LinkDir) -lGL -lglfw3 -lpthread -lm -lX11 -lXi -lXrandr -ldl

DS = GLFW.d

Objects  = GLFW.o
Objects += glad.o 
Objects += imgui.o
Objects += imgui_demo.o
Objects += imgui_draw.o
Objects += imgui_tables.o
Objects += imgui_widgets.o
Objects += imgui_impl_glfw.o
Objects += imgui_impl_opengl3.o

Project: $(Objects); -@ $(CC) $(Objects) $(LinkFlag) -o $(Target)

Auto  = @set -e; rm -f $@;
Auto += $(CC) -MM $(InclDir) $< > $@.$$$$;
Auto += sed 's,\($*\)\.o[ :]*,\1.o $@ : ,g' < $@.$$$$ > $@;
Auto += rm -f $@.$$$$

Compile = -@ $(CC) -o $@ $< $(CompFlag)

GLFW.o: Source/GLFW.cpp; $(Compile)
GLFW.d: Source/GLFW.cpp; $(Auto)

glad.o:               Support/Include/glad/glad.c; $(Compile)
imgui.o:              Support/Include/ImGUI/imgui.cpp;         $(Compile)
imgui_demo.o:         Support/Include/ImGUI/imgui_demo.cpp;    $(Compile)
imgui_draw.o:         Support/Include/ImGUI/imgui_draw.cpp;    $(Compile)
imgui_tables.o:       Support/Include/ImGUI/imgui_tables.cpp;  $(Compile)
imgui_widgets.o:      Support/Include/ImGUI/imgui_widgets.cpp; $(Compile)
imgui_impl_glfw.o:    Support/Include/ImGUI/backends/imgui_impl_glfw.cpp;    $(Compile)
imgui_impl_opengl3.o: Support/Include/ImGUI/backends/imgui_impl_opengl3.cpp; $(Compile)

-include $(DS)

clean: ; -@ rm $(Target) $(Objects) $(DS) $(DS:.d=.d.*) imgui.ini 2> /dev/null

