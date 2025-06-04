# Anya-Prompt-Monolith-Baseline
Anya implemented as a monolithic prompt


This is the baseline by which we will compare more complex Anya implementations. Autoregressive transformers condition token n+1 on all tokens up until n. So I first implemented anya as a giant prompt that sequentially processed information back to its context window. Worked ok, but no programatic control and will get slow and uwieldy and epensive. This is based on claude v3.  Both Opus and Haiku did ok, except the latter didn't follow directions as well regarding new words. Possibly can be solved using prompt engineering. 

Right now it's just a prompt text file and example conversation. Needs to be   a proper package with a UI so stakeholders can compare. 
