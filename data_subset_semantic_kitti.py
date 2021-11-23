import random
import shutil
import os
# total num for train 18840
# total num for val 4350
TOTAL_TRAIN = 18840
TOTAL_VAL = 4350
NUM_TRAIN = int(1884/2)
NUM_VAL = int(435/2)

def split_subdataset():
    lines_train = open(
        r'configs\datasets\semantic_kitti\train.txt').read().splitlines()
    selected_train_lines = random.sample(lines_train, NUM_TRAIN)
    with open('configs\datasets\semantic_kitti\sub_train_{NUM_TRAIN}.txt'.format(NUM_TRAIN=NUM_TRAIN), 'w') as f:
        for train_line in selected_train_lines:
            f.write(train_line)
            f.write('\n')

    lines_val = open(
        r'configs\datasets\semantic_kitti\val.txt').read().splitlines()
    selected_val_lines = random.sample(lines_val, NUM_VAL)
    with open('configs\datasets\semantic_kitti\sub_val_{NUM_VAL}.txt'.format(NUM_VAL=NUM_VAL), 'w') as f:
        for val_line in selected_val_lines:
            f.write(val_line)
            f.write('\n')

def copy_subdataset():
    input_root="./data/semantic_kitti/"
    target_root="../sub_dataset/"

    lines_train = open(
        'configs/datasets/semantic_kitti/sub_train_{NUM_TRAIN}.txt'.format(NUM_TRAIN=NUM_TRAIN)).read().splitlines()
    for line in lines_train:
        input_file_path=os.path.join(input_root,line)
        ouput_file_path=os.path.join(target_root,line)
        os.makedirs(os.path.dirname(ouput_file_path), exist_ok=True)
        shutil.copy(input_file_path, ouput_file_path)

    lines_val = open(
        'configs/datasets/semantic_kitti/sub_val_{NUM_VAL}.txt'.format(NUM_VAL=NUM_VAL)).read().splitlines()
    for line in lines_val:
        input_file_path=os.path.join(input_root,line)
        ouput_file_path=os.path.join(target_root,line)
        os.makedirs(os.path.dirname(ouput_file_path), exist_ok=True)
        shutil.copy(input_file_path, ouput_file_path)
    

#copy_subdataset()
split_subdataset()