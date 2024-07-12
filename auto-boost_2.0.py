import statistics
from math import ceil
import json
import sys
import subprocess
import vapoursynth as vs
import psutil
import os

core = vs.core
WORKERS = psutil.cpu_count(logical=False)


def print_help():
    print(
        'Usage:\npython auto-boost_2.0.py "{animu.mkv}" {base CQ/CRF/Q}"\n\nExample:\npython "auto-boost_2.0.py" "path/to/nice_boat.mkv" 30'
    )
    exit(0)


def get_ranges(scenes):
    ranges = []
    ranges.insert(0, 0)
    with open(scenes, "r") as file:
        content = json.load(file)
        for i in range(len(content["scenes"])):
            ranges.append(content["scenes"][i]["end_frame"])
    return ranges


def zones_txt(beginning_frame, end_frame, cq, zones_loc, iter):
    with open(zones_loc, "w" if iter == 1 else "a") as file:
        file.write(f"{beginning_frame} {end_frame} svt-av1 --crf {cq:.2f}\n")


def calculate_standard_deviation(score_list: list[int]):
    filtered_score_list = [score for score in score_list if score >= 0]
    sorted_score_list = sorted(filtered_score_list)
    average = sum(filtered_score_list) / len(filtered_score_list)
    return (average, sorted_score_list[len(filtered_score_list) // 20])


def fast_pass_encode(input_file, og_cq):
    fast_pass_command = f'av1an -i "{input_file}" --temp "{input_file[:-4]}/temp/" -y \
                        --verbose -k -m lsmash \
                        -c mkvmerge --sc-downscale-height 720 \
                        -e svt-av1 --force -v=" \
                        --preset 9 --crf {og_cq} --lp 2 \
                        --keyint -1 --fast-decode 1 --color-primaries 1 \
                        --transfer-characteristics 1 --matrix-coefficients 1" \
                        -w {WORKERS} \
                        -o "{input_file[:-4]}_fastpass.mkv"'
    p = subprocess.Popen(fast_pass_command, shell=True)
    exit_code = p.wait()
    if exit_code != 0:
        print("Av1an encountered an error during fast pass encode, exiting.")
        exit(-2)


def final_pass_encode(input_file, scenes_loc):
    final_pass_command = f'av1an -i "{input_file}" -o "{input_file[:-4]}_finalpass.mkv" \
                        --zones "{scenes_loc[:-11]}zones.txt" -e svt-av1 \
                        -v="--keyint -1 --tune 3 --enable-tf 0 --crf 30 --preset 4 \
                        --color-primaries 1 --transfer-characteristics 1 \
                        --matrix-coefficients 1" -m lsmash -c mkvmerge --verbose -w {WORKERS}'
    print(f"Final pass command: {final_pass_command}")
    p = subprocess.Popen(final_pass_command, shell=True)
    exit_code = p.wait()
    if exit_code != 0:
        print("Av1an encountered an error during final pass encoding, exiting.")
        exit(-3)


def process_video_files(input_file, scenes_loc, og_cq, br):
    src = core.lsmas.LWLibavSource(source=input_file, cache=0)
    enc = core.lsmas.LWLibavSource(source=f"{input_file[:-4]}_fastpass.mkv", cache=0)

    print(f"source: {len(src)} frames")
    print(f"encode: {len(enc)} frames")

    source_clip = src.resize.Bicubic(format=vs.RGBS, matrix_in_s="709").fmtc.transfer(
        transs="srgb", transd="linear", bits=32
    )
    encoded_clip = enc.resize.Bicubic(format=vs.RGBS, matrix_in_s="709").fmtc.transfer(
        transs="srgb", transd="linear", bits=32
    )

    return source_clip, encoded_clip


def calculate_ssim2_scores(ranges, source_clip, encoded_clip, skip):
    percentile_5_total = []
    total_ssim_scores = []
    for i in range(len(ranges) - 1):
        cut_source_clip = source_clip[ranges[i] : ranges[i + 1]].std.SelectEvery(
            cycle=skip, offsets=0
        )
        cut_encoded_clip = encoded_clip[ranges[i] : ranges[i + 1]].std.SelectEvery(
            cycle=skip, offsets=0
        )
        result = cut_source_clip.ssimulacra2.SSIMULACRA2(cut_encoded_clip)
        chunk_ssim_scores = []

        for frame in result.frames():
            score = frame.props["_SSIMULACRA2"]
            chunk_ssim_scores.append(score)
            total_ssim_scores.append(score)

        (average, percentile_5) = calculate_standard_deviation(chunk_ssim_scores)
        percentile_5_total.append(percentile_5)
    return total_ssim_scores, percentile_5_total


def adjust_cq(ranges, percentile_5_total, average, og_cq, br, scenes_loc):
    iter = 0
    for i in range(len(ranges) - 1):
        iter += 1
        new_cq = og_cq - ceil((1.0 - (percentile_5_total[i] / average)) / 0.5 * 40) / 4
        if new_cq < og_cq - br:
            new_cq = og_cq - br
        if new_cq > og_cq + br:
            new_cq = og_cq + br
        print(
            f"Enc:  [{ranges[i]}:{ranges[i+1]}]\n"
            f"Chunk 5th percentile: {percentile_5_total[i]}\n"
            f"Adjusted CRF: {new_cq}\n\n"
        )
        zones_txt(
            ranges[i], ranges[i + 1], new_cq, f"{scenes_loc[:-11]}zones.txt", iter
        )


def main():
    if "--help" in sys.argv[1:]:
        print_help()

    input_file = sys.argv[1]
    og_cq = float(sys.argv[2])  # CQ to start from
    br = 10  # maximum CQ change from original

    if not os.path.exists(f"{input_file[:-4]}_fastpass.mkv"):
        fast_pass_encode(input_file, og_cq)

    scenes_loc = f"{input_file[:-4]}/temp/scenes.json"
    ranges = get_ranges(scenes_loc)

    source_clip, encoded_clip = process_video_files(input_file, scenes_loc, og_cq, br)

    skip = 10  # amount of skipped frames
    total_ssim_scores, percentile_5_total = calculate_ssim2_scores(
        ranges, source_clip, encoded_clip, skip
    )

    (average, _) = calculate_standard_deviation(total_ssim_scores)
    print(f"Median score:  {average}\n\n")

    adjust_cq(ranges, percentile_5_total, average, og_cq, br, scenes_loc)

    if not os.path.exists(f"{input_file[:-4]}_finalpass.mkv"):
        final_pass_encode(input_file, scenes_loc)


if __name__ == "__main__":
    main()
