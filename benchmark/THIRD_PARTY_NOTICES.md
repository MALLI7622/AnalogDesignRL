# Third-party notices

This benchmark includes selected amplifier netlists and fixed parameter files from [CODA-Team/AnalogGym](https://github.com/CODA-Team/AnalogGym/tree/0a9d1390ade361e2b4a2d33181e22367edbb8afc), pinned commit `0a9d1390ade361e2b4a2d33181e22367edbb8afc`. Source paths and SHA-256 hashes are recorded in `benchmark/research/paper_candidates.json`, `benchmark/domains.json`, and each selected circuit's provenance. Copied circuit files are under `circuits/benchmark_*/`; the original checkout is under `external/AnalogGym/`.

The AnalogGym license below applies to the upstream software artifacts and their derivatives. Keep this notice with distributions containing those artifacts, including benchmark packages that embed their netlist text. Project-authored tasks, simulation conditions, parameter bounds, target requirements and evaluation records are additional work; the original paper authors and CODA-Team do not endorse them.

Circuit paper citations identify research provenance. No original paper PDF, scanned page or original publication figure is included for redistribution as part of this benchmark. Locally downloaded PDFs, text extractions and rendered pages under `runs/research_sources_20260910/` are research working files and must be excluded from release packages. The upstream software license does not grant rights to those publication assets. The existing AutoCkt circuit is a project-authored reconstruction described in `circuits/autockt_two_stage/source.json`, rather than a copied AnalogGym netlist. SKY130 model files remain governed by their own upstream licenses and dependency provenance; this notice does not relicense them.

## AnalogGym — BSD 3-Clause License

The following license text is copied verbatim from `external/AnalogGym/LICENSE` at the pinned commit:

```text
BSD 3-Clause License

Copyright (c) 2024, CODA-Team

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
