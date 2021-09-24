all: Engine;

Objects  = EngineCore.o
Objects += EngineCoreEditorUI.o

Include = -I ../Support/Include
Link = -l thread

Flags = $(Include) $(Link) -Wall -O2 -c --std=c++17

Engine: $(Objects); 

g++ -MM EngineCore.cpp; -@ g++ $(Flags) EngineCore.cpp

clean:; -@ rm ./*.o Engine