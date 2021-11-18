import random
# total num for train 18840
# total num for val 4350
NUM_TRAIN = int(1884/2)
NUM_VAL = int(435/2)

lines_train = open(
    r'configs\datasets\semantic_kitti\train.txt').read().splitlines()
with open('configs\datasets\semantic_kitti\sub_train_{NUM_TRAIN}.txt'.format(NUM_TRAIN=NUM_TRAIN), 'w') as f:
    for i in range(NUM_TRAIN):
        train_line = random.choice(lines_train)
        f.write(train_line)
        f.write('\n')

lines_val = open(
    r'configs\datasets\semantic_kitti\val.txt').read().splitlines()
with open('configs\datasets\semantic_kitti\sub_val_{NUM_VAL}.txt'.format(NUM_VAL=NUM_VAL), 'w') as f:
    for i in range(NUM_VAL):
        val_line = random.choice(lines_val)
        f.write(val_line)
        f.write('\n')
