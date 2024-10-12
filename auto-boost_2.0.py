import statistics
from math import ceil
import json
import sys
import subprocess
import os
import vapoursynth as vs

core = vs.core

if "--help" in sys.argv[1:]:
    print('Usage:\npython auto-boost_2.0.py "{animu.mkv}" {base CQ/CRF/Q}"\n\nExample:\npython "auto-boost_2.0.py" "path/to/nice_boat.mkv" 30')
    exit(0)

og_cq = float(sys.argv[2])  # CQ to start from
br = 10.0  # maximum CQ change from original

def get_ranges(scenes):
    ranges = [0]
    with open(scenes, "r") as file:
        content = json.load(file)
        for scene in content['scenes']:
            ranges.append(scene['end_frame'])
    return ranges

iter = 0
def zones_txt(beginning_frame, end_frame, cq, zones_loc):
    global iter
    iter += 1
    mode = "w" if iter == 1 else "a"
    with open(zones_loc, mode) as file:
        file.write(f"{beginning_frame} {end_frame} svt-av1 --crf {cq:.2f}\n")

def calculate_statistics(score_list):
    filtered_scores = [score for score in score_list if score >= 0]
    if not filtered_scores:
        return 0, 0
    average = statistics.mean(filtered_scores)
    percentile_5 = statistics.quantiles(filtered_scores, n=20)[0]
    return average, percentile_5

input_file = sys.argv[1]
output_dir = os.path.dirname(input_file)
temp_dir = os.path.join(output_dir, "temp")
output_file = os.path.join(output_dir, f"{os.path.splitext(os.path.basename(input_file))[0]}_fastpass.mkv")

fast_av1an_command = [
    'av1an',
    '-i', input_file,
    '--temp', temp_dir,
    '-y',
    '--verbose',
    '--keep',
    '-m', 'lsmash',
    '-c', 'mkvmerge',
    '--set-thread-affinity', '2',
    '-e', 'svt-av1',
    '--force',
    '-v', f'--preset 9 --crf {og_cq:.2f} --lp 2 --keyint -1 --fast-decode 1 --color-primaries 1 --transfer-characteristics 1 --matrix-coefficients 1',
    '--pix-format', 'yuv420p10le',
    '-w', '6',  # Adjust the number of workers as needed
    '-o', output_file
]

process = subprocess.run(fast_av1an_command, check=True)

if process.returncode != 0:
    print("Av1an encountered an error, exiting.")
    exit(-2)

scenes_loc = os.path.join(temp_dir, "scenes.json")
ranges = get_ranges(scenes_loc)

source_clip = core.lsmas.LWLibavSource(source=input_file, cache=0)
encoded_clip = core.lsmas.LWLibavSource(source=output_file, cache=0)

print(f"source: {len(source_clip)} frames")
print(f"encode: {len(encoded_clip)} frames")

percentile_5_total = []
total_ssim_scores = []

skip = 11  # amount of skipped frames

for i in range(len(ranges) - 1):
    cut_source_clip = source_clip[ranges[i]:ranges[i+1]].std.SelectEvery(cycle=skip, offsets=0)
    cut_encoded_clip = encoded_clip[ranges[i]:ranges[i+1]].std.SelectEvery(cycle=skip, offsets=0)
    result = core.vszip.Metrics(cut_source_clip, cut_encoded_clip, mode=0)
    chunk_ssim_scores = [frame.props['_SSIMULACRA2'] for frame in result.frames()]
    
    total_ssim_scores.extend(chunk_ssim_scores)
    average, percentile_5 = calculate_statistics(chunk_ssim_scores)
    percentile_5_total.append(percentile_5)

average, percentile_5 = calculate_statistics(total_ssim_scores)
print(f'Median score: {average:.4f}\n')

zones_file = os.path.join(temp_dir, "zones.txt")

for i in range(len(ranges) - 1):
    new_cq = og_cq - ceil((1.0 - (percentile_5_total[i] / average)) * 40 * 4) / 4 # trust me bro, change 40 to 20 for trix's but quarter-step
    new_cq = max(og_cq - br, min(new_cq, og_cq + br))

    print(f'Enc: [{ranges[i]}:{ranges[i+1]}]\n'
          f'Chunk 5th percentile: {percentile_5_total[i]:.4f}\n'
          f'Adjusted CRF: {new_cq:.2f}\n')
    
    zones_txt(ranges[i], ranges[i+1], new_cq, zones_file)

# yes, this is messier than the 1.0 code, laziness won over me, deal with it
